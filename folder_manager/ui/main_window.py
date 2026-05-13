import os
import shutil
import subprocess
import sys
import json
import importlib
import re
import tempfile
import time
import traceback
import io
import contextlib
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QSizePolicy, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QDialog, QProgressBar, QAbstractItemView, QComboBox, QTableWidget, QTableWidgetItem,
    QFrame, QSplitter,
    QPlainTextEdit, QFileDialog
)
from PySide6.QtCore import Qt, QTimer, QElapsedTimer, QThread, Signal, QEvent, QSettings, QSize, QSortFilterProxyModel, QModelIndex
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from .drag_list_widget import DragListWidget, ROLE_DB_ID, ROLE_DISK_NAME
from .dialog_system import (
    center_dialog as center_dialog_shared,
    dialog_dedupe_key as dialog_dedupe_key_shared,
    make_dialog_dedupe_key as make_dialog_dedupe_key_shared,
    show_dialog_topmost_non_modal as show_dialog_topmost_non_modal_shared,
    show_message_box_topmost_non_modal as show_message_box_topmost_non_modal_shared,
)
from ..db import DB, STAGES, WORKFLOW_DOMAIN_PREPAID, detect_stage_from_disk_name, normalize_display_name, parse_job_code
from ..workflow_logger import AuditEvent, write_event
from ..config import DB_HOST, DB_NAME, DB_USER, DB_PASS, DB_PORT, DEFAULT_PSD_DIR

ORDER_IMPORT_EXIT_CANCELLED = 41
FOLDER_ASSET_HINT_TEXT = (
    "Linked: single-click open. Not linked: Make Orders/QR single-click generate, PDF/Excel single-click choose.\n"
    "Not linked double-click chooses file. Use + to show extra slots for Make Orders/PDF. Red x clears link. "
    "Late PDF appears automatically after a late Gmail order. Excel: A=name, B=class, C=password (blank class uses sheet name)."
)


def _append_ui_runtime_log(message: str) -> None:
    try:
        log_path = Path(tempfile.gettempdir()) / "DAMYComp_ui_runtime.log"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {message}\n")
    except Exception:
        pass


class _IoTaskThread(QThread):
    def __init__(self, task):
        super().__init__()
        self._task = task
        self.result = None
        self.error: Exception | None = None
        self.traceback_text = ""

    def run(self) -> None:  # type: ignore[override]
        try:
            self.result = self._task()
        except Exception as exc:
            self.error = exc
            self.traceback_text = traceback.format_exc()


class _AssetPathBlacklistProxyModel(QSortFilterProxyModel):
    def __init__(self, blocked_keys: set[str], parent=None):
        super().__init__(parent)
        self._blocked_keys = set(blocked_keys)

    def _normalize(self, path_text: str) -> str:
        return str(path_text or "").replace("\\", "/").lower()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # type: ignore[override]
        model = self.sourceModel()
        if model is None:
            return True
        index = model.index(source_row, 0, source_parent)
        if not index.isValid():
            return True
        try:
            file_path = model.filePath(index)
            if not file_path:
                return True
            try:
                if Path(file_path).is_dir():
                    return True
            except Exception:
                pass
            return self._normalize(file_path) not in self._blocked_keys
        except Exception:
            return True


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):  # type: ignore[override]
        event.ignore()


class AssetDropZone(QFrame):
    clicked = Signal()
    doubleClicked = Signal()
    clearRequested = Signal()
    fileDropped = Signal(str)

    def __init__(self, asset_name: str):
        super().__init__()
        self.asset_name = asset_name
        self.setObjectName("assetDropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(180)
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
        self.clear_button.setObjectName("assetClearButton")
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.setFixedSize(18, 18)
        self.clear_button.setVisible(False)
        self.clear_button.setToolTip("Clear saved link")
        self.clear_button.clicked.connect(self.clearRequested.emit)
        top_row.addWidget(self.clear_button)
        layout.addLayout(top_row)

        self.title_label = QLabel(asset_name)
        self.title_label.setObjectName("assetTitle")
        self.title_label.setAlignment(Qt.AlignCenter)

        self.status_label = QLabel("Not linked")
        self.status_label.setObjectName("assetStatus")
        self.status_label.setAlignment(Qt.AlignCenter)

        self.hint_label = QLabel("Drop a file here\nor click to choose")
        self.hint_label.setObjectName("assetHint")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("assetMeta")
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
            "QFrame#assetDropZone {"
            f"background: {bg_color};"
            f"border: 2px {border_style} {border_color};"
            "border-radius: 12px; }"
            "QLabel#assetTitle { color: #eef3f9; font-size: 13px; font-weight: 700; border: none; background: transparent; }"
            "QLabel#assetStatus { color: #d9e6f7; font-size: 12px; font-weight: 600; border: none; background: transparent; }"
            "QLabel#assetHint { color: #9fb0c4; font-size: 11px; border: none; background: transparent; }"
            "QLabel#assetMeta { color: #7f93ad; font-size: 10px; border: none; background: transparent; }"
            "QPushButton#assetClearButton {"
            " min-height: 18px; max-height: 18px; min-width: 18px; max-width: 18px;"
            " margin: 0; padding: 0 0 1px 0; border-radius: 9px;"
            " border: 1px solid #cf6a6a; background: #a84444;"
            " color: #fff6f6; font-size: 10px; font-weight: 700; text-align: center; }"
            "QPushButton#assetClearButton:hover { background: #c45454; border-color: #e08383; }"
            "QPushButton#assetClearButton:pressed { background: #8f3838; }"
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
            self.hint_label.setText(f"{file_name}\nSingle-click to open")
        else:
            self.hint_label.setText("Drop a file here\nor click to choose")
        self.meta_label.setText(meta_text or "")
        self.clear_button.setVisible(bool(file_name))
        self._set_visual_state(linked=linked)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            interval = QApplication.instance().doubleClickInterval() if QApplication.instance() is not None else 250
            self._click_timer.start(max(150, int(interval) + 20))
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

    def dragEnterEvent(self, event: QDragEnterEvent):  # type: ignore[override]
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

    def dropEvent(self, event: QDropEvent):  # type: ignore[override]
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


class DragDropFolders(QWidget):
    def __init__(
        self,
        db_host: str = DB_HOST,
        dbname: str = DB_NAME,
        user: str = DB_USER,
        password: str = DB_PASS,
        port: int = DB_PORT,
    ):
        super().__init__()

        self.db = DB(host=db_host, dbname=dbname, user=user, password=password, port=port)
        self.db_name_display = str(dbname or os.environ.get("DAMY_DB_NAME") or DB_NAME or "unknown")
        self.current_user = os.environ.get("USERNAME") or os.environ.get("USER") or "UNKNOWN"
        self.source_base_dir = (os.environ.get("DAMY_SOURCE_BASE_DIR") or "").strip()
        self.hybrid_ui_marker = (os.environ.get("DAMY_UI_HYBRID_MARKER") or "").strip().lower() in {
            "1", "true", "yes", "y", "on"
        }
        self._active_top_dialog: QDialog | None = None
        self._open_top_dialogs: dict[str, QDialog] = {}

        try:
            self.base_dir = self._run_db_operation_with_retry(
                "get_base_dir",
                lambda: self.db.get_base_dir(),
                attempts=6,
                initial_delay_seconds=0.3,
            )
        except Exception as e:
            self._show_message_box_topmost_non_modal(
                QMessageBox.Icon.Critical,
                "DB Error",
                f"Could not read app_settings.base_directory.\n\n{e}",
            )
            raise

        if self.hybrid_ui_marker:
            self.setWindowTitle("DAMYComp [HYBRID: READ DAMY / WRITE DAMY_TEST]")
        else:
            self.setWindowTitle("DAMYComp")
        self.resize(1100, 560)
        self.expanded_column = None

        self.main_layout = QVBoxLayout(self)
        self.setLayout(self.main_layout)

        self.column_widgets = []
        self.edit_widgets = None  # (lw_edit, lw_i, lw_g)
        self.calendar_import_process = None
        self.calendar_import_dialog = None
        self.calendar_import_status_label = None
        self.calendar_import_timer = None
        self.calendar_import_elapsed = None
        self.calendar_import_launching = False
        self.order_import_process = None
        self.order_import_dialog = None
        self.order_import_status_label = None
        self.order_import_timer = None
        self.order_import_elapsed = None
        self.order_import_launching = False
        self._active_operations: dict[str, str] = {}
        self.operation_status_label: QLabel | None = None
        self.import_calendar_button: QPushButton | None = None
        self.import_order_button: QPushButton | None = None
        self.newly_added_item_ids: set[int] = set()
        self.moved_item_ids: set[int] = set()
        self.updated_item_ids: set[int] = set()
        self._calendar_import_before_snapshot: dict[str, int] = {}
        self._order_import_before_snapshot: dict[str, int] = {}
        self.order_import_log_path: str | None = None
        self.order_import_log_handle = None
        self.order_import_cancel_token_path: str | None = None
        self._close_requested_while_running = False
        self.folder_action_popup: QFrame | None = None
        self.folder_action_popup_host_list: DragListWidget | None = None
        self.folder_action_popup_host_item: QListWidgetItem | None = None
        self.folder_action_popup_host_widget: QWidget | None = None
        self.folder_action_anchor_list: DragListWidget | None = None
        self.folder_action_anchor_item_id: int | None = None
        self.folder_action_anchor_db_item = None
        self.folder_action_anchor_disk_name: str = ""
        self.folder_action_make_orders_zone: AssetDropZone | None = None
        self.folder_action_make_orders_zone_2: AssetDropZone | None = None
        self.folder_action_make_orders_zone_3: AssetDropZone | None = None
        self.folder_action_make_orders_zone_4: AssetDropZone | None = None
        self.folder_action_make_orders_add_button: QPushButton | None = None
        self.folder_action_qr_roster_zone: AssetDropZone | None = None
        self.folder_action_qr_orders_zone: AssetDropZone | None = None
        self.folder_action_pdf_zone: AssetDropZone | None = None
        self.folder_action_pdf_zone_2: AssetDropZone | None = None
        self.folder_action_pdf_zone_3: AssetDropZone | None = None
        self.folder_action_pdf_zone_4: AssetDropZone | None = None
        self.folder_action_late_pdf_zone: AssetDropZone | None = None
        self.folder_action_pdf_add_button: QPushButton | None = None
        self.folder_action_excel_zone: AssetDropZone | None = None
        self._folder_action_show_orders_form_slot2 = False
        self._folder_action_show_orders_form_slot3 = False
        self._folder_action_show_orders_form_slot4 = False
        self._folder_action_show_pdf_slot2 = False
        self._folder_action_show_pdf_slot3 = False
        self._folder_action_show_pdf_slot4 = False
        self._folder_action_show_late_pdf = False
        self._folder_action_pdf_click_guard_until = 0.0
        self.folder_action_inline_warning_label: QLabel | None = None
        self._asset_scan_worker: _IoTaskThread | None = None
        self._asset_scan_generation_token: int = 0
        self._asset_scan_pending_payload: list[
            tuple[int, str, str, str, str, str, str, str, str, str, str, str, str]
        ] | None = None
        self._asset_scan_pending_generation: int = 0
        self.column_splitter: QSplitter | None = None
        self._restoring_column_sizes = False
        self._last_db_issue_transient = False
        self._retry_status_text = ""
        self._completion_status_text = ""
        self._refresh_completion_pending = False
        self._completion_status_timer = QTimer(self)
        self._completion_status_timer.setSingleShot(True)
        self._completion_status_timer.timeout.connect(lambda: self._set_completion_status(""))
        self._refresh_retry_remaining = 0
        self._refresh_retry_timer = QTimer(self)
        self._refresh_retry_timer.setSingleShot(True)
        self._refresh_retry_timer.timeout.connect(self._retry_refresh_after_db_reconnect)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self.initialize_ui()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F5:
            self._on_refresh_clicked()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):  # type: ignore[override]
        self._hide_folder_action_popup()
        _append_ui_runtime_log(
            f"main window closeEvent active_operations={list(self._active_operations.keys())}"
        )
        if self._active_operations:
            _append_ui_runtime_log("closeEvent requested while operation is active; sending stop request")
            self._close_requested_while_running = True
            self._request_stop_active_operations()
            # If child processes already exited, release operation locks immediately.
            if not self._is_process_alive(self.calendar_import_process):
                self._end_operation("calendar_import")
            if not self._is_process_alive(self.order_import_process):
                self._end_operation("order_import")
            if not self._active_operations:
                _append_ui_runtime_log("all active operations stopped during closeEvent; closing now")
                event.accept()
                return
            self._refresh_operation_status_ui()
            event.ignore()
            return
        try:
            stack = "".join(traceback.format_stack(limit=10))
            _append_ui_runtime_log("closeEvent stack:\n" + stack)
        except Exception:
            pass
        try:
            self._save_column_splitter_sizes()
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass
        super().closeEvent(event)

    def eventFilter(self, watched, event):  # type: ignore[override]
        try:
            if (
                event is not None
                and event.type() == QEvent.Type.MouseButtonPress
                and self._should_hide_folder_action_popup_for_click(event)
            ):
                self._hide_folder_action_popup(clear_selection=True)
            elif (
                event is not None
                and event.type() == QEvent.Type.Resize
                and self.folder_action_popup_host_list is not None
                and watched in {self.folder_action_popup_host_list, self.folder_action_popup_host_list.viewport()}
            ):
                self._resize_folder_action_popup_host()
        except Exception:
            pass
        return super().eventFilter(watched, event)

    def _should_hide_folder_action_popup_for_click(self, event) -> bool:
        panel = self.folder_action_popup
        host_widget = self.folder_action_popup_host_widget
        anchor_list = self.folder_action_anchor_list
        if panel is None or not panel.isVisible():
            return False

        global_pos = event.globalPosition().toPoint()
        clicked_widget = QApplication.widgetAt(global_pos)
        if clicked_widget is not None:
            try:
                if panel is clicked_widget or panel.isAncestorOf(clicked_widget):
                    return False
            except Exception:
                pass
            if host_widget is not None:
                try:
                    if host_widget is clicked_widget or host_widget.isAncestorOf(clicked_widget):
                        return False
                except Exception:
                    pass
            if anchor_list is not None:
                try:
                    if anchor_list is clicked_widget or anchor_list.isAncestorOf(clicked_widget):
                        return False
                except Exception:
                    pass

        active_modal = QApplication.activeModalWidget()
        if active_modal is not None and active_modal.isVisible():
            try:
                if active_modal.rect().contains(active_modal.mapFromGlobal(global_pos)):
                    return False
            except Exception:
                pass
        stale_keys: list[str] = []
        for dlg_key, dlg in list(self._open_top_dialogs.items()):
            if dlg is None:
                stale_keys.append(str(dlg_key))
                continue
            try:
                if dlg.isVisible() and dlg.rect().contains(dlg.mapFromGlobal(global_pos)):
                    return False
            except Exception:
                stale_keys.append(str(dlg_key))
        for dlg_key in stale_keys:
            self._open_top_dialogs.pop(dlg_key, None)
        if host_widget is not None and host_widget.isVisible() and host_widget.rect().contains(host_widget.mapFromGlobal(global_pos)):
            return False
        if anchor_list is not None and anchor_list.isVisible() and anchor_list.rect().contains(anchor_list.mapFromGlobal(global_pos)):
            return False
        if panel.rect().contains(panel.mapFromGlobal(global_pos)):
            return False

        return True

    def _is_process_alive(self, proc) -> bool:
        try:
            return bool(proc and proc.poll() is None)
        except Exception:
            return False

    def _terminate_process_with_escalation(
        self,
        proc,
        *,
        label: str,
        graceful_wait_seconds: float = 1.6,
        kill_wait_seconds: float = 1.0,
    ) -> bool:
        if not self._is_process_alive(proc):
            return True

        try:
            proc.terminate()
            _append_ui_runtime_log(f"{label} terminate requested")
        except Exception as exc:
            _append_ui_runtime_log(f"{label} terminate failed: {exc}")

        deadline = time.time() + max(0.1, float(graceful_wait_seconds))
        while time.time() < deadline:
            if not self._is_process_alive(proc):
                _append_ui_runtime_log(f"{label} exited after terminate")
                return True
            self._yield_ui()
            time.sleep(0.05)

        try:
            proc.kill()
            _append_ui_runtime_log(f"{label} kill requested after terminate timeout")
        except Exception as exc:
            _append_ui_runtime_log(f"{label} kill failed: {exc}")

        deadline = time.time() + max(0.1, float(kill_wait_seconds))
        while time.time() < deadline:
            if not self._is_process_alive(proc):
                _append_ui_runtime_log(f"{label} exited after kill")
                return True
            self._yield_ui()
            time.sleep(0.05)

        still_alive = self._is_process_alive(proc)
        if still_alive:
            _append_ui_runtime_log(f"{label} still alive after terminate/kill attempts")
        return not still_alive

    def _request_stop_active_operations(self) -> None:
        # Order import supports cooperative cancellation+rollback via token file.
        try:
            proc = self.order_import_process
            if proc and proc.poll() is None:
                token_path = (self.order_import_cancel_token_path or "").strip()
                if token_path:
                    try:
                        Path(token_path).write_text("cancel", encoding="utf-8")
                        _append_ui_runtime_log(f"order import cancel token written: {token_path}")
                    except Exception as exc:
                        _append_ui_runtime_log(f"failed to write order import cancel token: {exc}")
        except Exception:
            pass

        # Calendar import has no transactional rollback; terminate process directly.
        try:
            proc = self.calendar_import_process
            if proc and proc.poll() is None:
                self._terminate_process_with_escalation(proc, label="calendar import")
        except Exception as exc:
            _append_ui_runtime_log(f"calendar import terminate failed: {exc}")

    def _clear_layout(self, layout: QVBoxLayout | QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
                continue
            if child_layout is not None:
                self._clear_layout(child_layout)  # type: ignore[arg-type]
                child_layout.deleteLater()

    def _create_stage_action_button(self, text: str, tooltip: str, handler) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setStatusTip(tooltip)
        btn.clicked.connect(handler)
        return btn

    def _column_size_settings(self) -> QSettings:
        return QSettings("DAMYComp", "DAMYComp")

    def _save_column_splitter_sizes(self) -> None:
        splitter = self.column_splitter
        if splitter is None or self._restoring_column_sizes or self.expanded_column is not None:
            return
        sizes = [int(size) for size in splitter.sizes()]
        if not sizes or any(size <= 0 for size in sizes):
            return
        self._column_size_settings().setValue("ui/column_splitter_sizes", json.dumps(sizes))

    def _restore_column_splitter_sizes(self) -> None:
        splitter = self.column_splitter
        if splitter is None:
            return
        raw_value = self._column_size_settings().value("ui/column_splitter_sizes", "")
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
        self._hide_folder_action_popup()
        self._clear_layout(self.main_layout)

        if self.hybrid_ui_marker:
            db_name = (os.environ.get("DAMY_DB_NAME") or DB_NAME or "unknown").strip()
            read_path = self.source_base_dir or "T:\\DAMY"
            write_path = self.base_dir or "T:\\DAMY_TEST"
            hybrid_label = QLabel(
                f"HYBRID MODE: Read {read_path} | Write {write_path} | DB: {db_name}"
            )
            hybrid_label.setAlignment(Qt.AlignCenter)
            hybrid_label.setStyleSheet(
                "QLabel { color: #ffe082; font-weight: 700; background-color: #2f3a25; "
                "border: 1px solid #566b43; border-radius: 4px; padding: 4px; }"
            )
            self.main_layout.addWidget(hybrid_label)

        self.total_label = QLabel("Total: 0 folders")
        self.total_label.setAlignment(Qt.AlignCenter)
        self.main_layout.addWidget(self.total_label)

        self.operation_status_label = QLabel("")
        self.operation_status_label.setAlignment(Qt.AlignCenter)
        self.operation_status_label.setWordWrap(True)
        self.operation_status_label.setStyleSheet(
            "QLabel { color: #90caf9; font-weight: 700; "
            "background-color: #1f2b36; border: 1px solid #35506b; "
            "border-radius: 4px; padding: 4px; }"
        )
        self.operation_status_label.setVisible(False)
        self.main_layout.addWidget(self.operation_status_label)

        toolbar_row = QHBoxLayout()
        toolbar_row.addStretch(1)
        btn_refresh = self._create_stage_action_button(
            "Refresh Workflow",
            "Re-read DB rows, compare DAMY folders, and refresh the board.",
            self._on_refresh_clicked,
        )
        toolbar_row.addWidget(btn_refresh)
        self.main_layout.addLayout(toolbar_row)

        refresh_help = QLabel("Refresh checks DAMY folders against the workflow DB before reloading the board.")
        refresh_help.setWordWrap(True)
        refresh_help.setStyleSheet("QLabel { color: #aab4c2; font-size: 11px; }")
        self.main_layout.addWidget(refresh_help)

        self.search_input = QLineEdit(
            placeholderText="Search by folder name, school name, PID, or progress text..."
        )
        self.search_input.setToolTip("Filters the visible board as you type.")
        self.search_input.textChanged.connect(self.perform_search)
        self.main_layout.addWidget(self.search_input)

        self.container = QWidget()
        self.container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        self.column_splitter = QSplitter(Qt.Horizontal)
        self.column_splitter.setChildrenCollapsible(False)
        self.column_splitter.setHandleWidth(10)
        self.column_splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.column_splitter.splitterMoved.connect(lambda _pos, _index: self._save_column_splitter_sizes())
        container_layout.addWidget(self.column_splitter)

        self.column_widgets = []
        self.edit_widgets = None
        self.import_calendar_button = None
        self.import_order_button = None
        self._asset_scan_generation_token += 1
        scan_generation = int(self._asset_scan_generation_token)
        items_by_stage = self._safe_list_all_by_stage()

        for stage_def in STAGES:
            stage = stage_def.stage
            day = stage_def.label
            items = list(items_by_stage.get(int(stage), []))
            item_count = len(items)

            if stage_def.edit_column:
                edit_column = QWidget()
                edit_layout = QVBoxLayout(edit_column)
                edit_layout.setContentsMargins(0, 0, 0, 0)

                label = QLabel(f"{day} ({item_count} items)")
                label.setAlignment(Qt.AlignCenter)
                label.mousePressEvent = self.make_toggle_column_visibility(edit_column)
                edit_layout.addWidget(label)

                lw_edit = DragListWidget(self.base_dir, day, requires_flags_to_leave=True)
                self._wire_draglistwidget_db(lw_edit, stage)

                lw_i = QListWidget()
                lw_g = QListWidget()
                lw_i.setFixedWidth(50)
                lw_g.setFixedWidth(50)

                for it in items:
                    self._add_db_item_to_list(lw_edit, it)

                    for lw_box, label_text, checked in [(lw_i, "I", it.flag_i), (lw_g, "G", it.flag_g)]:
                        cb_item = QListWidgetItem("✅" if checked else label_text)
                        cb_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                        cb_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                        lw_box.addItem(cb_item)

                lw_i.itemChanged.connect(self.handle_i_checkbox)
                lw_g.itemChanged.connect(self.handle_g_checkbox)

                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(lw_edit)
                row_layout.addWidget(lw_i)
                row_layout.addWidget(lw_g)

                edit_layout.addWidget(row_widget)
                self.column_splitter.addWidget(edit_column)

                self.column_widgets.append((edit_column, lw_edit))
                self.edit_widgets = (lw_edit, lw_i, lw_g)

                lw_edit.uiReloadRequested.connect(self.reload_ui)
                lw_edit.folderDropped.connect(self.on_folder_dropped)
                if self._stage_supports_folder_assets(stage):
                    self._wire_folder_action_popup(lw_edit)

            else:
                column = QWidget()
                column_layout = QVBoxLayout(column)
                column_layout.setContentsMargins(0, 0, 0, 0)

                label = QLabel(f"{day} ({item_count} items)")
                label.setAlignment(Qt.AlignCenter)
                label.mousePressEvent = self.make_toggle_column_visibility(column)
                column_layout.addWidget(label)

                if stage == 1:
                    btn = self._create_stage_action_button(
                        "Import Calendar Events",
                        "Create or update workflow folders from the DAMY calendar feed.",
                        self.run_upcoming_calendar_import,
                    )
                    column_layout.addWidget(btn)
                    self.import_calendar_button = btn

                    order_btn = self._create_stage_action_button(
                        "Import Gmail Orders",
                        "Read the Gmail label and import order PDFs into DAMY.",
                        self.run_order_import_from_gmail,
                    )
                    column_layout.addWidget(order_btn)
                    self.import_order_button = order_btn

                    action_help = QLabel(
                        "Calendar: create or update DAMY folders.\n"
                        "Gmail: import order PDFs from GODADDY ORDER, then move them to GODADDY IMPORTED."
                    )
                    action_help.setWordWrap(True)
                    action_help.setStyleSheet("QLabel { color: #aab4c2; font-size: 11px; }")
                    column_layout.addWidget(action_help)

                lw = DragListWidget(self.base_dir, day)
                self._wire_draglistwidget_db(lw, stage)

                for it in items:
                    self._add_db_item_to_list(lw, it)

                lw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                column_layout.addWidget(lw)
                self.column_splitter.addWidget(column)

                self.column_widgets.append((column, lw))
                lw.uiReloadRequested.connect(self.reload_ui)
                lw.folderDropped.connect(self.on_folder_dropped)
                if self._stage_supports_folder_assets(stage):
                    self._wire_folder_action_popup(lw)

        self.main_layout.addWidget(self.container, 1)
        QTimer.singleShot(0, self._restore_column_splitter_sizes)

        total_items = sum(lw.count() for _, lw in self.column_widgets)
        self.total_label.setText(f"Total: {total_items} folders")
        self._start_or_queue_asset_scan(
            self._build_stage1_asset_scan_payload(items_by_stage),
            generation=scan_generation,
        )
        self._set_retry_status("")
        self._refresh_operation_status_ui()
        self._set_operation_buttons_enabled(True)

    def run_upcoming_calendar_import(self):
        is_frozen = bool(getattr(sys, "frozen", False))
        root_dir = Path(__file__).resolve().parents[2]
        if is_frozen:
            root_dir = Path(sys.executable).resolve().parent

        if not self._try_begin_operation("calendar_import", "Calendar Import"):
            return

        self.calendar_import_launching = True
        process_started = False
        try:
            if not self._run_pre_import_sync_checks():
                return
            self._calendar_import_before_snapshot = self._fetch_visible_item_snapshot()
            if is_frozen:
                cmd = [sys.executable, "--calendar-import", "--skip-cancellation"]
            else:
                cmd = [sys.executable, "-m", "folder_manager.calendar_import_v3.main", "--skip-cancellation"]
            proc = subprocess.Popen(cmd, cwd=str(root_dir))
            process_started = True
            self.calendar_import_process = proc
            self._show_calendar_import_dialog(proc.pid or 0)
            self._start_calendar_import_monitor()
        except Exception as e:
            self._show_user_error_dialog(
                "Calendar Import Could Not Start",
                "DAMYComp could not start Calendar Import.",
                possible_causes=[
                    "The program files are incomplete or locked.",
                    "Windows blocked the subprocess from starting.",
                ],
                next_steps=[
                    "Close any old DAMYComp windows and try again.",
                    "If it still fails, reopen the launcher and test again.",
                ],
                details=str(e),
            )
        finally:
            self.calendar_import_launching = False
            if not process_started:
                self._end_operation("calendar_import")

    def _show_calendar_import_dialog(self, _pid: int):
        if self.calendar_import_dialog and self.calendar_import_dialog.isVisible():
            try:
                self.calendar_import_dialog.raise_()
                self.calendar_import_dialog.activateWindow()
            except Exception:
                pass
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Calendar Import Running")
        dlg.setModal(False)
        dlg.setMinimumWidth(360)

        layout = QVBoxLayout(dlg)
        status = QLabel("Calendar Import is running...\nElapsed: 0s")
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)

        layout.addWidget(status)
        layout.addWidget(bar)
        self._attach_responsive_font_scaling(dlg, text_widgets=[status], button_widgets=[], base_width=420)

        dlg.show()

        self.calendar_import_dialog = dlg
        self.calendar_import_status_label = status
        self.calendar_import_elapsed = QElapsedTimer()
        self.calendar_import_elapsed.start()

    def run_order_import_from_gmail(self):
        base_dir = self._resolve_order_import_base_dir()
        if not base_dir:
            self._show_user_error_dialog(
                "Order Import Blocked",
                "DAMYComp could not find a valid DAMY working folder.",
                possible_causes=[
                    "The workflow DB does not have a valid base directory.",
                    "The DAMY network path is not available on this computer.",
                ],
                next_steps=[
                    "Check app_settings.base_directory.",
                    "Make sure the DAMY folder is reachable, then try again.",
                ],
            )
            return

        is_frozen = bool(getattr(sys, "frozen", False))
        root_dir = Path(__file__).resolve().parents[2]
        if is_frozen:
            root_dir = Path(sys.executable).resolve().parent

        if not self._try_begin_operation("order_import", "Order Import"):
            return

        no_label_update = False
        max_messages = 0

        # Keep import markers scoped to current order-import run.
        self.updated_item_ids = set()
        self.newly_added_item_ids = set()
        self._order_import_before_snapshot = self._fetch_visible_item_snapshot()
        self.order_import_launching = True
        process_started = False
        try:
            if is_frozen:
                cmd = [
                    sys.executable,
                    "--order-import",
                    "--base-dir",
                    base_dir,
                ]
            else:
                cmd = [
                    sys.executable,
                    "-m",
                    "folder_manager.order_import_v1.main",
                    "--base-dir",
                    base_dir,
                ]
            if no_label_update:
                cmd.append("--no-label-update")
            if max_messages > 0:
                cmd.extend(["--label-window", str(max_messages), "--max-messages", str(max_messages)])
            source_base = (self.source_base_dir or "").strip()
            if source_base and os.path.isdir(source_base):
                cmd.extend(["--source-base-dir", source_base])
            cancel_tmp = tempfile.NamedTemporaryFile(prefix="order_import_cancel_", suffix=".token", delete=False)
            self.order_import_cancel_token_path = cancel_tmp.name
            cancel_tmp.close()
            try:
                os.remove(self.order_import_cancel_token_path)
            except Exception:
                pass
            cmd.extend(["--cancel-token-path", self.order_import_cancel_token_path])
            log_tmp = tempfile.NamedTemporaryFile(prefix="order_import_", suffix=".log", delete=False)
            self.order_import_log_path = log_tmp.name
            log_tmp.close()
            self.order_import_log_handle = open(self.order_import_log_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                cwd=str(root_dir),
                stdout=self.order_import_log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            process_started = True
            self.order_import_process = proc
            self._show_order_import_dialog(proc.pid or 0)
            self._start_order_import_monitor()
        except Exception as e:
            self._show_user_error_dialog(
                "Gmail Import Could Not Start",
                "DAMYComp could not start Import Gmail Orders.",
                possible_causes=[
                    "The program files are incomplete or locked.",
                    "Windows blocked the subprocess from starting.",
                ],
                next_steps=[
                    "Close any old DAMYComp windows and try again.",
                    "If the problem continues, reopen the launcher and test again.",
                ],
                details=str(e),
            )
        finally:
            self.order_import_launching = False
            if not process_started:
                if self.order_import_log_handle:
                    try:
                        self.order_import_log_handle.close()
                    except Exception:
                        pass
                    self.order_import_log_handle = None
                if self.order_import_log_path:
                    try:
                        os.remove(self.order_import_log_path)
                    except Exception:
                        pass
                    self.order_import_log_path = None
                if self.order_import_cancel_token_path:
                    try:
                        os.remove(self.order_import_cancel_token_path)
                    except Exception:
                        pass
                    self.order_import_cancel_token_path = None
                self._end_operation("order_import")

    def _resolve_order_import_base_dir(self) -> str | None:
        env_candidates = [
            (os.environ.get("DAMY_ORDER_BASE_DIR") or "").strip(),
            (os.environ.get("DAMY_BASE_DIR") or "").strip(),
        ]
        for candidate in env_candidates:
            if not candidate:
                continue
            if os.path.isdir(candidate):
                return candidate

        current = (self.base_dir or "").strip()
        normalized = current.replace("/", "\\").lower()
        if current and re.search(r"(^|\\)damy(?=\\|$)", normalized) and os.path.isdir(current):
            return current

        if current:
            replaced = re.sub(r"(?i)(^|\\)damy_test(?=\\|$)", r"\1DAMY", current, count=1)
            if replaced != current and os.path.isdir(replaced):
                return replaced
        return None

    def _show_order_import_dialog(self, _pid: int):
        if self.order_import_dialog and self.order_import_dialog.isVisible():
            try:
                self.order_import_dialog.raise_()
                self.order_import_dialog.activateWindow()
            except Exception:
                pass
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Order Import Running")
        dlg.setModal(False)
        dlg.setMinimumWidth(360)

        layout = QVBoxLayout(dlg)
        status = QLabel("Order Import is running...\nElapsed: 0s")
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)

        layout.addWidget(status)
        layout.addWidget(bar)
        self._attach_responsive_font_scaling(dlg, text_widgets=[status], button_widgets=[], base_width=420)

        dlg.show()

        self.order_import_dialog = dlg
        self.order_import_status_label = status
        self.order_import_elapsed = QElapsedTimer()
        self.order_import_elapsed.start()

    def _start_order_import_monitor(self):
        if self.order_import_timer is None:
            self.order_import_timer = QTimer(self)
            self.order_import_timer.timeout.connect(self._poll_order_import_process_safe)
        self.order_import_timer.start(500)

    def _poll_order_import_process_safe(self) -> None:
        try:
            self._poll_order_import_process()
        except KeyboardInterrupt:
            _append_ui_runtime_log("order import monitor interrupted by KeyboardInterrupt")
        except Exception as exc:
            _append_ui_runtime_log(f"order import monitor crashed: {exc}\n{traceback.format_exc()}")
            try:
                if self.order_import_timer:
                    self.order_import_timer.stop()
                if self.order_import_dialog:
                    self.order_import_dialog.close()
                if self.order_import_log_handle:
                    self.order_import_log_handle.close()
                    self.order_import_log_handle = None
            except Exception:
                pass
            self.order_import_process = None
            self.order_import_dialog = None
            self.order_import_status_label = None
            self.order_import_elapsed = None
            self._show_message_box_topmost_non_modal(
                QMessageBox.Icon.Warning,
                "Order Import",
                f"Order Import monitor encountered an unexpected error.\n\n{exc}",
            )
            self._end_operation("order_import")

    def _poll_order_import_process(self):
        proc = self.order_import_process
        if not proc:
            return
        completion_status_text = ""

        if self.order_import_status_label and self.order_import_elapsed:
            seconds = int(self.order_import_elapsed.elapsed() / 1000)
            self.order_import_status_label.setText(
                f"Order Import is running...\nElapsed: {seconds}s"
            )

        exit_code = proc.poll()
        if exit_code is None:
            return

        if self.order_import_timer:
            self.order_import_timer.stop()

        if self.order_import_dialog:
            self.order_import_dialog.close()

        if self.order_import_log_handle:
            try:
                self.order_import_log_handle.close()
            except Exception:
                pass
            self.order_import_log_handle = None

        self.order_import_process = None
        self.order_import_dialog = None
        self.order_import_status_label = None
        self.order_import_elapsed = None
        if self.order_import_cancel_token_path:
            try:
                os.remove(self.order_import_cancel_token_path)
            except Exception:
                pass
            self.order_import_cancel_token_path = None

        log_path = (self.order_import_log_path or "").strip()
        summary = self._parse_order_import_summary_from_log()
        touched_count = 0
        if summary:
            touched_count = self._apply_order_import_summary_markers(summary)

        # Refresh so newly created/updated folders appear in UI right away.
        self.reload_ui()
        if self._close_requested_while_running:
            self._end_operation("order_import")
            return
        if exit_code == 0:
            auto_qr_summary = self._auto_regenerate_qr_after_order_import(summary)
            if auto_qr_summary:
                if summary is None:
                    summary = {}
                summary["auto_qr"] = auto_qr_summary
                self.reload_ui()
            fail_count = int((summary or {}).get("processed_failed", 0))
            self._show_info_message_dialog(
                "Order Import",
                self._format_order_import_completion_message(summary),
                min_width=560,
                min_height=260,
            )
            if fail_count > 0:
                details = list((summary or {}).get("failure_details") or [])
                detail_text = self._format_order_import_failure_details(details)
                self._show_info_text_dialog("Orders Needing Review", detail_text)
            completion_status_text = "Import Gmail Orders complete."
        elif exit_code == ORDER_IMPORT_EXIT_CANCELLED or bool((summary or {}).get("cancelled", False)):
            rollback_applied = int((summary or {}).get("rollback_applied", 0))
            rollback_errors = list((summary or {}).get("rollback_errors") or [])
            msg = (
                "Order Import cancelled.\n"
                "This run has been rolled back.\n"
                f"Rollback applied: {rollback_applied}\n"
                f"Rollback errors: {len(rollback_errors)}\n"
                f"Log: {log_path or '(none)'}"
            )
            self._show_info_message_dialog(
                "Order Import",
                msg,
                min_width=520,
                min_height=240,
            )
            if rollback_errors:
                self._show_info_text_dialog(
                    "Order Import Rollback Errors",
                    "\n".join(str(x) for x in rollback_errors[:200]),
                )
        else:
            self._show_message_box_topmost_non_modal(
                QMessageBox.Icon.Warning,
                "Order Import",
                f"Order Import exited with code {exit_code}.\nLog: {log_path or '(none)'}",
            )
        self._end_operation("order_import")
        if completion_status_text:
            self._set_completion_status(completion_status_text)

    def _parse_order_import_summary_from_log(self) -> dict | None:
        path = (self.order_import_log_path or "").strip()
        self.order_import_log_path = None
        if not path:
            return None
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

        marker = "__ORDER_IMPORT_SUMMARY__"
        parsed_summary: dict | None = None
        for line in reversed(text.splitlines()):
            if line.startswith(marker):
                raw = line[len(marker):]
                try:
                    parsed_summary = json.loads(raw)
                    break
                except Exception:
                    parsed_summary = None
                    break

        # Fallback/augment from runtime lines when summary is missing or incomplete.
        ok_folders: list[str] = []
        for line in text.splitlines():
            m = re.match(r"^\[OK\]\s+(.+?)\s+->\s+.+$", line.strip())
            if not m:
                continue
            folder = (m.group(1) or "").strip()
            if folder:
                ok_folders.append(folder)

        if parsed_summary is None and not ok_folders:
            return None

        summary = parsed_summary or {}
        touched = list(summary.get("touched_folders") or [])
        if not touched and ok_folders:
            summary["touched_folders"] = list(dict.fromkeys(ok_folders))
        if "processed_ok" not in summary or int(summary.get("processed_ok") or 0) == 0:
            if ok_folders:
                summary["processed_ok"] = len(ok_folders)
        if "created_folders" not in summary:
            summary["created_folders"] = []
        if "touched_item_ids" not in summary:
            summary["touched_item_ids"] = []
        if "created_item_ids" not in summary:
            summary["created_item_ids"] = []
        if "cancelled" not in summary:
            summary["cancelled"] = False
        if "rollback_applied" not in summary:
            summary["rollback_applied"] = 0
        if "rollback_errors" not in summary:
            summary["rollback_errors"] = []
        if "failure_details" not in summary:
            summary["failure_details"] = []
        if "processed_failed" not in summary:
            summary["processed_failed"] = len(list(summary.get("failure_details") or []))
        return summary

    def _format_order_import_completion_message(self, summary: dict | None) -> str:
        ok_count = int((summary or {}).get("processed_ok", 0))
        fail_count = int((summary or {}).get("processed_failed", 0))
        duplicate_count = int((summary or {}).get("duplicates_skipped", 0))
        no_label_update = bool((summary or {}).get("no_label_update", True))
        label_updates = int((summary or {}).get("label_updates", 0))
        moved_count = 0 if no_label_update else label_updates
        failed_line = (
            "1 email could not be imported."
            if fail_count == 1
            else f"{fail_count} emails could not be imported."
        )
        message = (
            "Order Import Complete\n\n"
            f"Imported: {ok_count}\n"
            f"Needs review: {fail_count}\n"
            f"Duplicates skipped: {duplicate_count}\n"
            f"Moved to Imported label: {moved_count}\n\n"
            f"{failed_line}"
        )
        auto_qr = (summary or {}).get("auto_qr") or {}
        if auto_qr:
            updated = int(auto_qr.get("updated", 0) or 0)
            skipped = int(auto_qr.get("skipped", 0) or 0)
            failed = int(auto_qr.get("failed", 0) or 0)
            message += (
                "\n\nAuto QR Update\n"
                f"Updated: {updated}\n"
                f"Skipped: {skipped}\n"
                f"Failed: {failed}"
            )
            failure_lines = list(auto_qr.get("failure_lines") or [])
            if failure_lines:
                preview = "\n".join(str(x) for x in failure_lines[:8])
                message += f"\n\nQR update issues:\n{preview}"
                if len(failure_lines) > 8:
                    message += f"\n...and {len(failure_lines) - 8} more"
        return message

    def _format_order_import_failure_details(self, details: list[dict]) -> str:
        blocks: list[str] = []
        for item in details:
            reason = str(item.get("reason", "")).strip()
            detail = str(item.get("detail", "")).strip()
            school = str(item.get("school_name", "")).strip()
            subject = str(item.get("subject", "")).strip()
            from_header = str(item.get("from_header", "")).strip()
            header_date = str(item.get("header_date", "")).strip()
            pid = str(item.get("pid", "") or "").strip()
            order_no = str(item.get("order_no", "") or "").strip()

            if not school:
                school_match = re.search(r"school='([^']+)'", detail)
                if school_match:
                    school = school_match.group(1).strip()
                    detail = re.sub(r"\s*school='[^']+'", "", detail).strip(" .")

            reason_lower = reason.lower()
            detail_lower = detail.lower()

            if (
                "manual selection was skipped" in detail_lower
                or "manual selection; user skipped" in detail_lower
            ):
                user_reason = "More than one folder matched this order."
            elif "pid not found in email" in reason_lower or "pid not found in email" in detail_lower:
                user_reason = "Picture Day ID was not found."
            elif "no existing damy folder matched pid" in detail_lower:
                user_reason = "Picture Day ID did not match an existing folder."
            elif "selected folder has no pid in its name" in detail_lower:
                user_reason = "The selected folder does not contain a PID."
            elif "stream has ended unexpectedly" in detail_lower:
                user_reason = "A PDF file could not be read."
            else:
                user_reason = reason or "The email could not be imported."
                if not user_reason.endswith("."):
                    user_reason += "."

            if (
                "manual folder selection was skipped" in detail_lower
                or "manual selection; user skipped" in detail_lower
            ):
                what_happened = (
                    "The import found multiple possible folders and needs someone to choose "
                    "the correct one before it can continue."
                )
            elif "stream has ended unexpectedly" in detail_lower:
                what_happened = (
                    "The order PDF could not be read completely. An existing PDF may be damaged, "
                    "or the shared drive may have interrupted the file."
                )
            else:
                what_happened = detail or "The email could not be imported."
                if not what_happened.endswith("."):
                    what_happened += "."

            lines = [
                "This order was not imported.",
                f"Reason: {user_reason}",
                f"What happened: {what_happened}",
            ]
            next_step = ""
            if (
                "manual folder selection was skipped" in detail_lower
                or "manual selection; user skipped" in detail_lower
            ):
                next_step = "Run the order import again and select the correct folder when asked."
            elif "pid not found in email" in reason_lower or "pid not found in email" in detail_lower:
                next_step = "Check the email/order for the correct Picture Day ID, then import it again."
            elif "no existing damy folder matched pid" in detail_lower:
                next_step = "Find or create the matching job folder, then import it again."
            elif "stream has ended unexpectedly" in detail_lower:
                next_step = "Check the existing order PDF, then import the order again."
            if next_step:
                lines.append(f"Next step: {next_step}")
            if subject:
                lines.append(f"Subject: {subject}")
            if from_header:
                lines.append(f"From: {from_header}")
            if header_date:
                lines.append(f"Email Date: {header_date}")
            if pid:
                lines.append(f"Picture Day ID: {pid}")
            if order_no:
                lines.append(f"Order: {order_no}")
            if school:
                lines.append(f"School: {school}")
            blocks.append("\n".join(lines))

        if not blocks:
            return "This order was not imported."
        return "\n\n".join(blocks[:200])

    def _apply_order_import_summary_markers(self, summary: dict) -> int:
        touched_folders = list(summary.get("touched_folders") or [])
        touched_item_ids_raw = list(summary.get("touched_item_ids") or [])
        created_folders = set(summary.get("created_folders") or [])
        created_item_ids_raw = list(summary.get("created_item_ids") or [])
        touched_ids: set[int] = set()
        new_ids: set[int] = set()
        visible_snapshot = self._fetch_visible_item_snapshot()
        visible_ids = {int(item_id) for item_id in visible_snapshot.values()}

        for raw_id in touched_item_ids_raw:
            try:
                parsed_id = int(raw_id)
            except Exception:
                continue
            if parsed_id in visible_ids:
                touched_ids.add(parsed_id)

        for raw_id in created_item_ids_raw:
            try:
                parsed_id = int(raw_id)
            except Exception:
                continue
            if parsed_id in visible_ids:
                new_ids.add(parsed_id)

        def _normalized_pid_set(text: str) -> set[str]:
            values: set[str] = set()
            for token in re.findall(r"\bP\d{7,}\b", (text or "").upper()):
                digits = token[1:]
                if len(digits) == 9 and digits.startswith("0"):
                    digits = digits[1:]
                if len(digits) == 8:
                    values.add(f"P{digits}")
            return values

        def _resolve_item_id(folder_name: str) -> int | None:
            name = (folder_name or "").strip()
            if not name:
                return None

            try:
                item = self.db.get_item_by_disk_name(name)
            except Exception:
                item = None
            if item:
                return int(item.id)

            lowered = name.lower()
            suffix_matches = [
                int(item_id)
                for disk_name, item_id in visible_snapshot.items()
                if str(disk_name).strip().lower().endswith(lowered)
            ]
            if len(suffix_matches) == 1:
                return suffix_matches[0]
            if suffix_matches:
                return suffix_matches[0]

            norm = normalize_display_name(name).strip().lower()
            if not norm:
                return None
            norm_matches = [
                int(item_id)
                for disk_name, item_id in visible_snapshot.items()
                if normalize_display_name(str(disk_name)).strip().lower() == norm
            ]
            if len(norm_matches) == 1:
                return norm_matches[0]
            if norm_matches:
                return norm_matches[0]

            folder_pids = _normalized_pid_set(name)
            if folder_pids:
                pid_matches = []
                for disk_name, item_id in visible_snapshot.items():
                    if folder_pids & _normalized_pid_set(str(disk_name)):
                        pid_matches.append(int(item_id))
                if len(pid_matches) == 1:
                    return pid_matches[0]
                if pid_matches:
                    return pid_matches[0]
            return None

        for folder_name in touched_folders:
            item_id = _resolve_item_id(str(folder_name))
            if item_id is None:
                continue
            touched_ids.add(item_id)
            if str(folder_name) in created_folders:
                new_ids.add(item_id)

        if not new_ids:
            after = self._fetch_visible_item_snapshot()
            before = self._order_import_before_snapshot or {}
            if before and after:
                new_ids |= {item_id for disk_name, item_id in after.items() if disk_name not in before}
            else:
                _append_ui_runtime_log(
                    "order import new-id fallback skipped: missing before/after snapshot"
                )

        self.updated_item_ids |= touched_ids
        self.newly_added_item_ids |= new_ids
        if touched_folders and not touched_ids:
            _append_ui_runtime_log(
                f"order import marker resolve miss: touched_folders={len(touched_folders)} resolved_ids=0"
            )
        return len(touched_ids)

    def _order_import_touched_item_ids_for_auto_qr(self, summary: dict | None) -> list[int]:
        if not summary or bool(summary.get("cancelled", False)):
            return []
        ids: set[int] = set()
        for raw_id in list(summary.get("touched_item_ids") or []):
            try:
                ids.add(int(raw_id))
            except Exception:
                continue
        if ids:
            return sorted(ids)
        for folder_name in list(summary.get("touched_folders") or []):
            name = str(folder_name or "").strip()
            if not name:
                continue
            try:
                item = self.db.get_item_by_disk_name(name)
            except Exception:
                item = None
            if item:
                ids.add(int(item.id))
        return sorted(ids)

    def _auto_regenerate_qr_after_order_import(self, summary: dict | None) -> dict | None:
        processed_ok = int((summary or {}).get("processed_ok", 0) or 0)
        if processed_ok <= 0:
            return None
        item_ids = self._order_import_touched_item_ids_for_auto_qr(summary)
        if not item_ids:
            return None
        try:
            return self._run_blocking_io_task(
                "Auto Update QR",
                (
                    "Import Gmail Orders updated order PDFs.\n\n"
                    f"Regenerating QR Orders and QR Roster for {len(item_ids)} folder(s)..."
                ),
                lambda: self._auto_regenerate_qr_for_item_ids(item_ids),
            )
        except Exception as exc:
            _append_ui_runtime_log(f"auto QR after order import failed: {exc}")
            return {
                "updated": 0,
                "skipped": 0,
                "failed": len(item_ids),
                "failure_lines": [f"Auto QR update failed before finishing: {exc}"],
            }

    def _run_targeted_order_import_for_qr(self, item_id: int) -> dict | None:
        base_dir = self._resolve_order_import_base_dir()
        if not base_dir:
            raise RuntimeError("Could not find a valid DAMY working folder for targeted Gmail import.")

        is_frozen = bool(getattr(sys, "frozen", False))
        root_dir = Path(__file__).resolve().parents[2]
        if is_frozen:
            root_dir = Path(sys.executable).resolve().parent

        cancel_tmp = tempfile.NamedTemporaryFile(prefix="order_import_qr_cancel_", suffix=".token", delete=False)
        cancel_token_path = cancel_tmp.name
        cancel_tmp.close()
        try:
            os.remove(cancel_token_path)
        except Exception:
            pass

        log_tmp = tempfile.NamedTemporaryFile(prefix="order_import_qr_", suffix=".log", delete=False)
        log_path = log_tmp.name
        log_tmp.close()

        try:
            if is_frozen:
                cmd = [
                    sys.executable,
                    "--order-import",
                    "--base-dir",
                    base_dir,
                    "--only-item-id",
                    str(int(item_id)),
                    "--cancel-token-path",
                    cancel_token_path,
                ]
            else:
                cmd = [
                    sys.executable,
                    "-m",
                    "folder_manager.order_import_v1.main",
                    "--base-dir",
                    base_dir,
                    "--only-item-id",
                    str(int(item_id)),
                    "--cancel-token-path",
                    cancel_token_path,
                ]
            source_base = (self.source_base_dir or "").strip()
            if source_base and os.path.isdir(source_base):
                cmd.extend(["--source-base-dir", source_base])

            with open(log_path, "w", encoding="utf-8") as log_fh:
                proc = subprocess.run(
                    cmd,
                    cwd=str(root_dir),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=900,
                )

            previous_log_path = self.order_import_log_path
            self.order_import_log_path = log_path
            try:
                summary = self._parse_order_import_summary_from_log()
            finally:
                self.order_import_log_path = previous_log_path

            if proc.returncode != 0:
                detail = ""
                try:
                    detail = Path(log_path).read_text(encoding="utf-8", errors="ignore")[-3000:]
                except Exception:
                    detail = ""
                raise RuntimeError(
                    f"Targeted Gmail import exited with code {proc.returncode}.\n{detail}".strip()
                )
            return summary
        finally:
            try:
                os.remove(cancel_token_path)
            except Exception:
                pass
            try:
                os.remove(log_path)
            except Exception:
                pass

    def _ensure_latest_orders_for_qr(self, item_id: int, qr_kind: str) -> dict | None:
        try:
            summary = self._run_blocking_io_task(
                "Check Gmail Orders",
                "Checking Gmail for new orders for this folder...\n\nIf new orders exist, DAMYComp will import them before updating QR.",
                lambda: self._run_targeted_order_import_for_qr(item_id),
            )
        except Exception as exc:
            self._show_user_error_dialog(
                "QR Update Blocked",
                "DAMYComp could not confirm whether Gmail has newer orders for this folder.",
                possible_causes=[
                    "Gmail authentication failed or was cancelled.",
                    "The Gmail label is unavailable.",
                    "The order import hit a PDF or folder matching error.",
                ],
                next_steps=[
                    "Run Import Gmail Orders manually and resolve any issues.",
                    "Then click QR Orders or QR Roster again.",
                ],
                details=str(exc),
            )
            return None

        fail_count = int((summary or {}).get("processed_failed", 0) or 0)
        if fail_count > 0:
            details = self._format_order_import_failure_details(
                list((summary or {}).get("failure_details") or [])
            )
            self._show_user_error_dialog(
                "QR Update Blocked",
                "Gmail has order email(s) for this folder, but at least one could not be imported.",
                next_steps=[
                    "Fix the import issue first.",
                    "Then click QR Orders or QR Roster again.",
                ],
                details=details,
            )
            return None

        auto_summary = self._auto_regenerate_qr_after_order_import(summary)
        if auto_summary:
            self._refresh_folder_action_asset_states(item_id)
            if int(auto_summary.get("failed", 0) or 0) > 0:
                failure_lines = "\n".join(str(x) for x in list(auto_summary.get("failure_lines") or [])[:20])
                self._show_user_error_dialog(
                    "QR Update Blocked",
                    "Gmail import found new orders, but DAMYComp could not regenerate the QR files safely.",
                    next_steps=[
                        "Review the QR update errors.",
                        "Fix the linked PDF/Excel files if needed, then click the QR button again.",
                    ],
                    details=failure_lines or str(auto_summary),
                )
                return None
            processed_ok = int((summary or {}).get("processed_ok", 0) or 0)
            if processed_ok > 0:
                wanted = "QR Roster" if str(qr_kind).strip().lower() == "roster" else "QR Orders"
                relevant_skips = [
                    str(line)
                    for line in list(auto_summary.get("skipped_lines") or [])
                    if wanted in str(line)
                ]
                if relevant_skips:
                    self._show_user_error_dialog(
                        "QR Update Blocked",
                        f"Gmail import found new orders, but {wanted} could not be regenerated.",
                        next_steps=[
                            "Link the required source files for this QR output.",
                            "Then click the QR button again.",
                        ],
                        details="\n".join(relevant_skips[:20]),
                    )
                    return None
        return summary or {}

    def _auto_qr_existing_target(
        self,
        db_item,
        *,
        qr_kind: str,
        folder_dir: Path,
        fallback_stem: str,
    ) -> tuple[Path, str]:
        asset_kind = "qr_roster" if qr_kind == "roster" else "qr_orders"
        raw = self._raw_asset_path_from_item(db_item, asset_kind)
        if raw:
            try:
                current = self._resolve_action_path(raw)
                if current.parent.exists():
                    return current.parent, current.stem
            except Exception:
                pass
        return folder_dir, fallback_stem

    def _auto_regenerate_qr_for_item_ids(self, item_ids: list[int]) -> dict:
        qr_module = importlib.import_module("folder_manager.qr_tags_v1.main")
        updated_lines: list[str] = []
        skipped_lines: list[str] = []
        failure_lines: list[str] = []

        for raw_item_id in item_ids:
            item_id = int(raw_item_id)
            try:
                db_item = self.db.get_item_by_id(item_id)
                if not db_item:
                    skipped_lines.append(f"item_id={item_id}: workflow row no longer exists")
                    continue
                disk_name = str(getattr(db_item, "disk_name", "") or "").strip()
                folder_dir = self._resolve_existing_folder_path(disk_name)
                if folder_dir is None:
                    skipped_lines.append(f"{disk_name or item_id}: folder not found")
                    continue

                excel_path: Path | None = None
                raw_excel = str(getattr(db_item, "excel_path", "") or "").strip()
                if raw_excel:
                    candidate = self._resolve_action_path(raw_excel)
                    if candidate.exists():
                        excel_path = candidate

                pdf_paths = self._collect_linked_pdf_paths_for_qr(db_item)

                if pdf_paths:
                    orders_base = f"{(excel_path.stem if excel_path is not None else folder_dir.name)}_qr_orders"
                    output_dir, output_stem = self._auto_qr_existing_target(
                        db_item,
                        qr_kind="orders",
                        folder_dir=folder_dir,
                        fallback_stem=orders_base,
                    )
                    result = qr_module.generate_qr_tags(
                        excel_path=str(excel_path) if excel_path is not None else None,
                        pdf_path=str(pdf_paths[0]),
                        pdf_paths=[str(p) for p in pdf_paths],
                        output_dir=str(output_dir),
                        mode="orders",
                        output_base_name=output_stem,
                    )
                    output_pdf = Path(str(result.output_pdf_path)).expanduser()
                    if not output_pdf.exists():
                        raise RuntimeError(f"QR Orders output missing: {output_pdf}")
                    self.db.set_qr_orders_path(item_id, str(output_pdf))
                    updated_lines.append(f"{disk_name}: QR Orders")
                else:
                    skipped_lines.append(f"{disk_name}: QR Orders skipped; no linked order PDF")

                if excel_path is not None:
                    roster_base = f"{excel_path.stem}_qr_roster"
                    output_dir, output_stem = self._auto_qr_existing_target(
                        db_item,
                        qr_kind="roster",
                        folder_dir=folder_dir,
                        fallback_stem=roster_base,
                    )
                    result = qr_module.generate_qr_tags(
                        excel_path=str(excel_path),
                        pdf_path=str(pdf_paths[0]) if pdf_paths else None,
                        pdf_paths=[str(p) for p in pdf_paths] if pdf_paths else None,
                        output_dir=str(output_dir),
                        mode="roster",
                        output_base_name=output_stem,
                    )
                    output_pdf = Path(str(result.output_pdf_path)).expanduser()
                    if not output_pdf.exists():
                        raise RuntimeError(f"QR Roster output missing: {output_pdf}")
                    self.db.set_qr_roster_path(item_id, str(output_pdf))
                    updated_lines.append(f"{disk_name}: QR Roster")
                else:
                    skipped_lines.append(f"{disk_name}: QR Roster skipped; no linked Excel")
            except Exception as exc:
                failure_lines.append(f"item_id={item_id}: {exc}")

        return {
            "updated": len(updated_lines),
            "skipped": len(skipped_lines),
            "failed": len(failure_lines),
            "updated_lines": updated_lines,
            "skipped_lines": skipped_lines,
            "failure_lines": failure_lines,
        }

    def _qr_source_paths_for_kind(self, db_item, qr_kind: str) -> list[Path]:
        kind = str(qr_kind or "").strip().lower()
        sources: list[Path] = []
        if kind == "roster":
            raw_excel = str(getattr(db_item, "excel_path", "") or "").strip()
            if raw_excel:
                excel_path = self._resolve_action_path(raw_excel)
                if excel_path.exists():
                    sources.append(excel_path)
        if kind in {"orders", "roster"}:
            sources.extend(self._collect_linked_pdf_paths_for_qr(db_item))
        return sources

    def _linked_qr_is_stale(self, db_item, qr_kind: str) -> bool:
        kind = str(qr_kind or "").strip().lower()
        asset_kind = "qr_roster" if kind == "roster" else "qr_orders"
        raw_qr = self._raw_asset_path_from_item(db_item, asset_kind)
        if not raw_qr:
            return False
        qr_path = self._resolve_action_path(raw_qr)
        if not qr_path.exists():
            return False
        try:
            qr_mtime = qr_path.stat().st_mtime
        except Exception:
            return True
        for source in self._qr_source_paths_for_kind(db_item, kind):
            try:
                if source.stat().st_mtime > qr_mtime + 1.0:
                    return True
            except Exception:
                return True
        return False

    def _start_calendar_import_monitor(self):
        if self.calendar_import_timer is None:
            self.calendar_import_timer = QTimer(self)
            self.calendar_import_timer.timeout.connect(self._poll_calendar_import_process_safe)
        self.calendar_import_timer.start(500)

    def _poll_calendar_import_process_safe(self) -> None:
        try:
            self._poll_calendar_import_process()
        except KeyboardInterrupt:
            _append_ui_runtime_log("calendar import monitor interrupted by KeyboardInterrupt")
        except Exception as exc:
            _append_ui_runtime_log(f"calendar import monitor crashed: {exc}\n{traceback.format_exc()}")
            try:
                if self.calendar_import_timer:
                    self.calendar_import_timer.stop()
                if self.calendar_import_dialog:
                    self.calendar_import_dialog.close()
            except Exception:
                pass
            self.calendar_import_process = None
            self.calendar_import_dialog = None
            self.calendar_import_status_label = None
            self.calendar_import_elapsed = None
            self._show_message_box_topmost_non_modal(
                QMessageBox.Icon.Warning,
                "Calendar Import",
                f"Calendar Import monitor encountered an unexpected error.\n\n{exc}",
            )
            self._end_operation("calendar_import")

    def _poll_calendar_import_process(self):
        proc = self.calendar_import_process
        if not proc:
            return
        completion_status_text = ""

        if self.calendar_import_status_label and self.calendar_import_elapsed:
            seconds = int(self.calendar_import_elapsed.elapsed() / 1000)
            self.calendar_import_status_label.setText(
                f"Calendar Import is running...\nElapsed: {seconds}s"
            )

        exit_code = proc.poll()
        if exit_code is None:
            return

        if self.calendar_import_timer:
            self.calendar_import_timer.stop()

        if self.calendar_import_dialog:
            self.calendar_import_dialog.close()

        self.calendar_import_process = None
        self.calendar_import_dialog = None
        self.calendar_import_status_label = None
        self.calendar_import_elapsed = None
        if self._close_requested_while_running:
            self._end_operation("calendar_import")
            return

        if exit_code == 0:
            imported_count = 0
            try:
                imported_count = self._compute_new_items_from_snapshot()
            except Exception:
                imported_count = 0

            # Refresh immediately so imported rows show up in UI before follow-up dialogs.
            self.reload_ui()
            self._show_info_message_dialog(
                "Calendar Import",
                f"Calendar Import finished.\nImported events: {imported_count}",
                min_width=420,
                min_height=200,
            )

            cancellation_changed = False
            try:
                cancellation_changed = self._run_cancellation_in_ui()
            except Exception as e:
                self._show_message_box_topmost_non_modal(
                    QMessageBox.Icon.Warning,
                    "Cancellation UI",
                    f"Import finished, but cancellation UI failed.\n\n{e}",
                )
            if cancellation_changed:
                self.reload_ui()
            completion_status_text = "Import Calendar Events complete."
        else:
            self._show_message_box_topmost_non_modal(
                QMessageBox.Icon.Warning,
                "Calendar Import",
                f"Calendar Import exited with code {exit_code}.",
            )
        self._end_operation("calendar_import")
        if completion_status_text:
            self._set_completion_status(completion_status_text)

    def _calendar_module(self):
        return importlib.import_module("folder_manager.calendar_import_v3.main")

    def _extract_cancellation_report_from_output(self, stdout_text: str, stderr_text: str = "") -> dict:
        marker = "__CANCELLATION_REPORT__"
        combined_lines = []
        if stdout_text:
            combined_lines.extend(str(stdout_text).splitlines())
        if stderr_text:
            combined_lines.extend(str(stderr_text).splitlines())

        for line in reversed(combined_lines):
            if line.startswith(marker):
                raw = line[len(marker):].strip()
                if raw:
                    return json.loads(raw)

        # Fallback: allow raw JSON output on a single line or as a whole buffer.
        combined_text = "\n".join(combined_lines).strip()
        if combined_text:
            for candidate in reversed([line.strip() for line in combined_lines if line.strip()]):
                if candidate.startswith("{") and candidate.endswith("}"):
                    return json.loads(candidate)
            if combined_text.startswith("{") and combined_text.endswith("}"):
                return json.loads(combined_text)

        raise RuntimeError("Cancellation report not found in subprocess output.")

    def _run_cancellation_report_in_process_raw(self):
        module = self._calendar_module()
        args = [
            "--run-cancellation",
            "--cancellation-report-json",
            "--base-dir",
            self.base_dir,
        ]
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            module.main(args)
        return self._extract_cancellation_report_from_output(
            stdout_buffer.getvalue(),
            stderr_buffer.getvalue(),
        )

    def _run_cancellation_report_subprocess_raw(self):
        is_frozen = bool(getattr(sys, "frozen", False))
        root_dir = Path(__file__).resolve().parents[2]
        if is_frozen:
            root_dir = Path(sys.executable).resolve().parent

        if is_frozen:
            cmd = [
                sys.executable,
                "--run-cancellation",
                "--cancellation-report-json",
                "--base-dir",
                self.base_dir,
            ]
        else:
            cmd = [
                sys.executable,
                "-m",
                "folder_manager.calendar_import_v3.main",
                "--run-cancellation",
                "--cancellation-report-json",
                "--base-dir",
                self.base_dir,
            ]

        _append_ui_runtime_log("cancellation report subprocess start")
        cp = subprocess.run(
            cmd,
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=600,
        )
        stdout_text = cp.stdout or ""
        stderr_text = cp.stderr or ""
        if cp.returncode == 0:
            try:
                report = self._extract_cancellation_report_from_output(stdout_text, stderr_text)
                _append_ui_runtime_log("cancellation report subprocess done")
                return report
            except Exception as exc:
                _append_ui_runtime_log(
                    "cancellation report subprocess parse miss; "
                    f"falling back to in-process run: {exc}; "
                    f"stdout_tail={(stdout_text[-400:] if stdout_text else '')!r}; "
                    f"stderr_tail={(stderr_text[-400:] if stderr_text else '')!r}"
                )
                return self._run_cancellation_report_in_process_raw()

        _append_ui_runtime_log(
            "cancellation report subprocess failed; "
            f"falling back to in-process run; returncode={cp.returncode}; "
            f"stdout_tail={(stdout_text[-400:] if stdout_text else '')!r}; "
            f"stderr_tail={(stderr_text[-400:] if stderr_text else '')!r}"
        )
        return self._run_cancellation_report_in_process_raw()

    def _run_cancellation_report_subprocess(self):
        try:
            return self._run_blocking_io_task(
                "Cancellation Scan",
                "Scanning cancellation mismatches...\nPlease wait.",
                self._run_cancellation_report_subprocess_raw,
            )
        except Exception as exc:
            raise RuntimeError(
                "Cancellation scan did not finish normally. "
                "Please check the DAMYComp runtime log for the subprocess output details."
            ) from exc

    def _center_dialog(self, dlg: QDialog):
        center_dialog_shared(dlg, self)

    def _dialog_dedupe_key(self, dlg: QDialog) -> str:
        return dialog_dedupe_key_shared(dlg)

    def _make_dialog_dedupe_key(self, namespace: str, *parts: object) -> str:
        return make_dialog_dedupe_key_shared(namespace, *parts)

    def _set_active_top_dialog(self, dlg: QDialog) -> None:
        self._active_top_dialog = dlg

    def _clear_active_top_dialog(self, dlg: QDialog) -> None:
        if self._active_top_dialog is dlg:
            self._active_top_dialog = None

    def _show_message_box_topmost_non_modal(
        self,
        icon: QMessageBox.Icon,
        title: str,
        text: str,
        *,
        parent: QWidget | None = None,
        buttons: QMessageBox.StandardButton = QMessageBox.Ok,
        default_button: QMessageBox.StandardButton = QMessageBox.Ok,
        button_labels: dict[int, str] | None = None,
        informative_text: str | None = None,
        detailed_text: str | None = None,
        dedupe_key: str | None = None,
    ) -> int:
        owner = parent if parent is not None else self
        return show_message_box_topmost_non_modal_shared(
            icon,
            title,
            text,
            parent=owner,
            buttons=buttons,
            default_button=default_button,
            button_labels=button_labels,
            informative_text=informative_text,
            detailed_text=detailed_text,
            dedupe_key=dedupe_key,
            open_dialogs=self._open_top_dialogs,
            set_active_dialog=self._set_active_top_dialog,
            clear_active_dialog=self._clear_active_top_dialog,
        )

    def _show_dialog_topmost_non_modal(self, dlg: QDialog) -> int:
        return show_dialog_topmost_non_modal_shared(
            dlg,
            anchor=self,
            open_dialogs=self._open_top_dialogs,
            set_active_dialog=self._set_active_top_dialog,
            clear_active_dialog=self._clear_active_top_dialog,
        )

    def _focus_active_operation_ui(self) -> None:
        if self.calendar_import_dialog and self.calendar_import_dialog.isVisible():
            try:
                self.calendar_import_dialog.showNormal()
            except Exception:
                pass
            self.calendar_import_dialog.raise_()
            self.calendar_import_dialog.activateWindow()
            return
        if self.order_import_dialog and self.order_import_dialog.isVisible():
            try:
                self.order_import_dialog.showNormal()
            except Exception:
                pass
            self.order_import_dialog.raise_()
            self.order_import_dialog.activateWindow()
            return
        if self._active_top_dialog and self._active_top_dialog.isVisible():
            self._active_top_dialog.raise_()
            self._active_top_dialog.activateWindow()

    def _refresh_operation_status_ui(self) -> None:
        if not self.operation_status_label:
            return
        running = [label for label in self._active_operations.values() if str(label).strip()]
        retry_text = (self._retry_status_text or "").strip()
        completion_text = (self._completion_status_text or "").strip()
        if running:
            unique = list(dict.fromkeys(running))
            if self._close_requested_while_running:
                if len(unique) == 1:
                    base_text = f"Stopping {unique[0]}..."
                else:
                    base_text = "Stopping: " + ", ".join(unique)
            elif len(unique) == 1:
                base_text = f"{unique[0]} is running..."
            else:
                base_text = "Running: " + ", ".join(unique)
            if retry_text:
                base_text = f"{base_text}\n{retry_text}"
            self.operation_status_label.setStyleSheet(
                "QLabel { color: #90caf9; font-weight: 700; "
                "background-color: #1f2b36; border: 1px solid #35506b; "
                "border-radius: 4px; padding: 4px; }"
            )
            self.operation_status_label.setText(base_text)
            self.operation_status_label.setVisible(True)
            return
        if retry_text:
            self.operation_status_label.setStyleSheet(
                "QLabel { color: #90caf9; font-weight: 700; "
                "background-color: #1f2b36; border: 1px solid #35506b; "
                "border-radius: 4px; padding: 4px; }"
            )
            self.operation_status_label.setText(retry_text)
            self.operation_status_label.setVisible(True)
            return
        if completion_text:
            self.operation_status_label.setStyleSheet(
                "QLabel { color: #d7f6df; font-weight: 700; "
                "background-color: #223428; border: 1px solid #4e7a58; "
                "border-radius: 4px; padding: 4px; }"
            )
            self.operation_status_label.setText(completion_text)
            self.operation_status_label.setVisible(True)
            return
        self.operation_status_label.setVisible(False)

    def _set_retry_status(self, text: str = "") -> None:
        self._retry_status_text = str(text or "").strip()
        self._refresh_operation_status_ui()

    def _set_completion_status(self, text: str = "", *, duration_ms: int = 3200) -> None:
        self._completion_status_text = str(text or "").strip()
        if self._completion_status_timer.isActive():
            self._completion_status_timer.stop()
        if self._completion_status_text:
            self._completion_status_timer.start(max(800, int(duration_ms)))
        self._refresh_operation_status_ui()

    def _set_operation_buttons_enabled(self, enabled: bool) -> None:
        if self.import_calendar_button:
            self.import_calendar_button.setEnabled(enabled)
        if self.import_order_button:
            self.import_order_button.setEnabled(enabled)

    def _try_begin_operation(self, key: str, label: str) -> bool:
        if key in self._active_operations:
            active_label = (self._active_operations.get(key) or label or "Program").strip()
            self._show_info_message_dialog(
                "Program Running",
                f"{active_label} is already running.\nPlease wait until it finishes.",
                min_width=420,
                min_height=200,
            )
            self._focus_active_operation_ui()
            return False
        self._active_operations[key] = label
        self._refresh_operation_status_ui()
        return True

    def _end_operation(self, key: str) -> None:
        if key in self._active_operations:
            self._active_operations.pop(key, None)
            self._refresh_operation_status_ui()
        if self._close_requested_while_running and not self._active_operations:
            self._close_requested_while_running = False
            QTimer.singleShot(0, self.close)

    def _attach_responsive_font_scaling(
        self,
        dlg: QDialog,
        text_widgets: list[QWidget],
        button_widgets: list[QPushButton],
        *,
        base_width: int = 900,
    ) -> None:
        def _apply() -> None:
            width = max(360, int(dlg.width() or base_width))
            scale = max(1.0, min(1.8, width / float(base_width)))
            text_pt = max(13, int(round(14 * scale)))
            button_pt = max(12, int(round(13 * scale)))
            button_h = max(34, int(round(34 * scale)))

            for w in text_widgets:
                try:
                    f = w.font()
                    f.setPointSize(text_pt)
                    w.setFont(f)
                    if isinstance(w, QTableWidget):
                        hf = w.horizontalHeader().font()
                        hf.setPointSize(max(12, text_pt - 1))
                        w.horizontalHeader().setFont(hf)
                except Exception:
                    pass

            for b in button_widgets:
                try:
                    f = b.font()
                    f.setPointSize(button_pt)
                    b.setFont(f)
                    b.setMinimumHeight(button_h)
                except Exception:
                    pass

        old_resize = dlg.resizeEvent

        def _resize(event):
            if callable(old_resize):
                old_resize(event)
            _apply()

        dlg.resizeEvent = _resize  # type: ignore[assignment]
        _apply()

    def _ask_question_topmost_non_modal(
        self,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButton,
        default_button: QMessageBox.StandardButton,
        *,
        button_labels: dict[int, str] | None = None,
        close_result: int | None = None,
    ) -> int:
        if close_result is None:
            close_result = int(QMessageBox.Cancel)

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setMinimumSize(760, 460)
        dlg.setProperty(
            "dialog_dedupe_key",
            self._make_dialog_dedupe_key(
                "question",
                title,
                text,
                int(buttons),
                int(default_button),
                json.dumps({str(k): str(v) for k, v in (button_labels or {}).items()}, sort_keys=True),
            ),
        )
        dlg.setStyleSheet(
            "QDialog { background: #20252e; color: #e8ecf1; }"
            "QPlainTextEdit { background: #151a21; color: #f4f7fb; border: 1px solid #3b4557; border-radius: 8px; }"
            "QPushButton { padding: 8px 14px; border-radius: 8px; border: 1px solid #3b4557; background: #2b3340; color: #f4f7fb; }"
            "QPushButton:hover { background: #364255; }"
        )

        layout = QVBoxLayout(dlg)
        text_view = QPlainTextEdit()
        text_view.setReadOnly(True)
        text_view.setPlainText(text or "")
        layout.addWidget(text_view)

        row = QHBoxLayout()
        row.addStretch(1)

        result = {"button": int(close_result)}
        button_widgets: list[QPushButton] = []

        button_defs = [
            (QMessageBox.Yes, "Yes"),
            (QMessageBox.No, "No"),
            (QMessageBox.Cancel, "Cancel"),
            (QMessageBox.Ok, "OK"),
        ]

        fallback_button = None
        for sb, label in button_defs:
            if not (buttons & sb):
                continue
            custom_label = (button_labels or {}).get(int(sb))
            btn = QPushButton(custom_label if custom_label else label)
            btn.setMinimumWidth(130)
            row.addWidget(btn)
            button_widgets.append(btn)
            if fallback_button is None:
                fallback_button = int(sb)
            if sb == default_button:
                btn.setDefault(True)
                btn.setFocus()

            def _choose(_checked=False, val=int(sb)):
                result["button"] = val
                dlg.accept()

            btn.clicked.connect(_choose)

        layout.addLayout(row)

        if fallback_button is not None and result["button"] == int(QMessageBox.Cancel):
            result["button"] = fallback_button

        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[text_view],
            button_widgets=button_widgets,
            base_width=900,
        )

        def _on_rejected() -> None:
            result["button"] = int(close_result)
        dlg.rejected.connect(_on_rejected)
        self._show_dialog_topmost_non_modal(dlg)
        return int(result["button"])

    def _show_info_text_dialog(
        self,
        title: str,
        text: str,
        *,
        min_width: int = 880,
        min_height: int = 560,
    ) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setMinimumSize(min_width, min_height)
        dlg.setProperty(
            "dialog_dedupe_key",
            self._make_dialog_dedupe_key("info_text", title, text),
        )
        dlg.setStyleSheet(
            "QDialog { background: #20252e; color: #e8ecf1; }"
            "QPlainTextEdit { background: #151a21; color: #f4f7fb; border: 1px solid #3b4557; border-radius: 8px; }"
            "QPushButton { padding: 8px 14px; border-radius: 8px; border: 1px solid #3b4557; background: #2b3340; color: #f4f7fb; }"
            "QPushButton:hover { background: #364255; }"
        )

        layout = QVBoxLayout(dlg)
        text_view = QPlainTextEdit()
        text_view.setReadOnly(True)
        text_view.setPlainText(text or "")
        layout.addWidget(text_view)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_ok = QPushButton("OK")
        btn_ok.clicked.connect(dlg.accept)
        row.addWidget(btn_ok)
        layout.addLayout(row)

        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[text_view],
            button_widgets=[btn_ok],
            base_width=max(720, min_width),
        )
        self._show_dialog_topmost_non_modal(dlg)

    def _show_info_message_dialog(
        self,
        title: str,
        message: str,
        *,
        min_width: int = 460,
        min_height: int = 220,
    ) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setMinimumSize(min_width, min_height)
        dlg.setProperty(
            "dialog_dedupe_key",
            self._make_dialog_dedupe_key("info_message", title, message),
        )
        dlg.setStyleSheet(
            "QDialog { background: #20252e; color: #e8ecf1; }"
            "QLabel#infoMessage { color: #f4f7fb; font-size: 14px; line-height: 1.35; background: transparent; }"
            "QPushButton { padding: 8px 16px; border-radius: 8px; border: 1px solid #3b4557; background: #2b3340; color: #f4f7fb; }"
            "QPushButton:hover { background: #364255; }"
        )

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(14)

        message_label = QLabel(str(message or "").strip())
        message_label.setObjectName("infoMessage")
        message_label.setWordWrap(True)
        message_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(message_label)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_ok = QPushButton("OK")
        btn_ok.clicked.connect(dlg.accept)
        row.addWidget(btn_ok)
        layout.addLayout(row)

        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[message_label],
            button_widgets=[btn_ok],
            base_width=max(420, min_width),
        )
        self._show_dialog_topmost_non_modal(dlg)

    def _show_user_error_dialog(
        self,
        title: str,
        happened: str,
        *,
        possible_causes: list[str] | None = None,
        next_steps: list[str] | None = None,
        details: str | None = None,
    ) -> None:
        sections = [f"What happened:\n{happened.strip()}"]
        if possible_causes:
            sections.append(
                "Possible reasons:\n"
                + "\n".join(f"- {line}" for line in possible_causes if str(line).strip())
            )
        if next_steps:
            sections.append(
                "What to do now:\n"
                + "\n".join(f"- {line}" for line in next_steps if str(line).strip())
            )
        if details:
            sections.append(f"Details:\n{str(details).strip()}")
        self._show_info_text_dialog(
            title,
            "\n\n".join(part for part in sections if part.strip()),
            min_width=760,
            min_height=420,
        )

    def _confirm_dangerous_action(
        self,
        *,
        title: str,
        happened: str,
        impacts: list[str],
        confirm_label: str,
        cancel_label: str = "Cancel",
    ) -> bool:
        message = (
            f"Please confirm this action:\n\n{happened.strip()}\n\n"
            "What this will do:\n"
            + "\n".join(f"- {line}" for line in impacts if str(line).strip())
        )
        answer = self._ask_question_topmost_non_modal(
            title,
            message,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
            button_labels={
                int(QMessageBox.Yes): confirm_label,
                int(QMessageBox.Cancel): cancel_label,
            },
            close_result=int(QMessageBox.Cancel),
        )
        return answer == int(QMessageBox.Yes)

    def _format_file_timestamp(self, path: Path | None) -> str:
        if path is None:
            return ""
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %I:%M %p")
        except Exception:
            return ""

    def _asset_allowed_suffixes(self, asset_kind: str) -> set[str]:
        kind = str(asset_kind or "").strip().lower()
        if kind in {"pdf", "pdf_2", "pdf_3", "pdf_4", "late_pdf", "qr_roster", "qr_orders"}:
            return {".pdf"}
        if kind == "excel":
            return {".xlsx", ".xls", ".xlsm", ".xlsb", ".csv"}
        if kind in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4"}:
            return {".jpg", ".jpeg"}
        return set()

    def _asset_filter_text(self, asset_kind: str) -> str:
        kind = str(asset_kind or "").strip().lower()
        if kind in {"pdf", "pdf_2", "pdf_3", "pdf_4", "late_pdf", "qr_roster", "qr_orders"}:
            return "PDF Files (*.pdf);;All Files (*.*)"
        if kind == "excel":
            return "Excel Files (*.xlsx *.xls *.xlsm *.xlsb);;CSV Files (*.csv);;All Files (*.*)"
        if kind in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4"}:
            return "JPG Files (*.jpg *.jpeg);;All Files (*.*)"
        return "All Files (*.*)"

    def _asset_kind_label(self, asset_kind: str) -> str:
        kind = str(asset_kind or "").strip().lower()
        labels = {
            "pdf": "PDF",
            "pdf_2": "PDF #2",
            "pdf_3": "PDF #3",
            "pdf_4": "PDF #4",
            "late_pdf": "Late PDF",
            "excel": "Excel",
            "orders_form": "Make Orders Form",
            "orders_form_2": "Make Orders Form #2",
            "orders_form_3": "Make Orders Form #3",
            "orders_form_4": "Make Orders Form #4",
            "qr_roster": "QR Roster",
            "qr_orders": "QR Orders",
        }
        return labels.get(kind, kind.upper())

    def _asset_allowed_text(self, asset_kind: str) -> str:
        kind = str(asset_kind or "").strip().lower()
        if kind in {"pdf", "pdf_2", "pdf_3", "pdf_4", "late_pdf", "qr_roster", "qr_orders"}:
            return ".pdf"
        if kind == "excel":
            return ".xlsx, .xls, .xlsm, .xlsb, or .csv"
        if kind in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4"}:
            return ".jpg or .jpeg"
        return "the expected file type"

    def _describe_asset_file(self, raw_path: str) -> tuple[Path | None, str | None, str | None, bool]:
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            return None, None, None, False
        path = self._resolve_action_path(cleaned)
        file_name = path.name or cleaned
        if path.exists():
            ts = self._format_file_timestamp(path)
            meta = f"Last updated: {ts}" if ts else "File found"
            return path, file_name, meta, True
        return path, file_name, "Saved path no longer exists", False

    def _asset_link_exists(self, raw_path: str) -> bool:
        return bool(self._describe_asset_file(raw_path)[3])

    def _ask_missing_action_qt(
        self,
        folder_name: str,
        school_name: str,
        yymmdd: str,
        base_path: str,
        reason_text: str | None = None,
        *,
        next_only: bool = False,
    ) -> str:
        dlg = QDialog(self)
        dlg.setWindowTitle("Calendar Mismatch")
        dlg.setModal(True)
        dlg.setMinimumWidth(520)
        dlg.setStyleSheet(
            "QDialog { background: #20252e; color: #e8ecf1; }"
            "QLabel#title { font-weight: 700; color: #f4f7fb; }"
            "QLabel#meta { color: #b9c3d1; }"
            "QPushButton { padding: 8px 16px; border-radius: 8px; border: 1px solid #3b4557; background: #2b3340; color: #f4f7fb; }"
            "QPushButton:hover { background: #364255; }"
            "QPushButton#cancelBtn { background: #5a2b2b; border-color: #8a4242; }"
            "QPushButton#cancelBtn:hover { background: #744040; }"
            "QPushButton#ignoreBtn { background: #2f3a4a; }"
        )

        layout = QVBoxLayout(dlg)
        title_text = reason_text or "No matching Calendar event found"
        if next_only:
            title_text += "\n\nPrevious choice kept. Use Next to continue."
        title = QLabel(title_text)
        title.setObjectName("title")
        layout.addWidget(title)

        meta = QLabel(
            f"Folder: {folder_name}\n"
            f"School: {school_name}\n"
            f"Date: {yymmdd}\n"
            f"Base: {base_path}"
        )
        meta.setObjectName("meta")
        meta.setWordWrap(True)
        layout.addWidget(meta)

        btn_row = QHBoxLayout()
        button_widgets: list[QPushButton] = []

        # Closing this dialog via X behaves like "Ignore" (normal) or "Next" (locked mode).
        result = {"value": "next" if next_only else "ignore"}

        def _choose_cancel(*_args):
            result["value"] = "cancel"
            dlg.accept()

        def _choose_reschedule(*_args):
            result["value"] = "reschedule"
            dlg.accept()

        def _choose_previous(*_args):
            result["value"] = "previous"
            dlg.accept()

        def _choose_ignore(*_args):
            result["value"] = "ignore"
            dlg.accept()

        def _choose_ignore_all(*_args):
            result["value"] = "ignore_all"
            dlg.accept()

        def _choose_next(*_args):
            result["value"] = "next"
            dlg.accept()

        if next_only:
            btn_next = QPushButton("Next")
            btn_next.setObjectName("ignoreBtn")
            btn_row.addWidget(btn_next)
            button_widgets = [btn_next]
            btn_next.clicked.connect(_choose_next)
            dlg.rejected.connect(_choose_next)
        else:
            btn_cancel = QPushButton("Cancel Event")
            btn_cancel.setObjectName("cancelBtn")
            btn_reschedule = QPushButton("Reschedule Event")
            btn_previous = QPushButton("Previous")
            btn_previous.setObjectName("ignoreBtn")
            btn_ignore = QPushButton("Ignore")
            btn_ignore.setObjectName("ignoreBtn")
            btn_ignore_all = QPushButton("Ignore All (This Reason)")
            btn_ignore_all.setObjectName("ignoreBtn")

            btn_row.addWidget(btn_cancel)
            btn_row.addWidget(btn_reschedule)
            btn_row.addWidget(btn_previous)
            btn_row.addWidget(btn_ignore)
            btn_row.addWidget(btn_ignore_all)
            button_widgets = [btn_cancel, btn_reschedule, btn_previous, btn_ignore, btn_ignore_all]

            btn_cancel.clicked.connect(_choose_cancel)
            btn_reschedule.clicked.connect(_choose_reschedule)
            btn_previous.clicked.connect(_choose_previous)
            btn_ignore.clicked.connect(_choose_ignore)
            btn_ignore_all.clicked.connect(_choose_ignore_all)
            dlg.rejected.connect(_choose_ignore)
        layout.addLayout(btn_row)
        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[title, meta],
            button_widgets=button_widgets,
            base_width=820,
        )

        self._show_dialog_topmost_non_modal(dlg)
        return result["value"]

    def _ask_reschedule_target_qt(self, school_name: str, current_folder: str, folder_names: list[str]):
        candidates = [name for name in folder_names if name != current_folder]
        if not candidates:
            return None

        dlg = QDialog(self)
        dlg.setWindowTitle("Reschedule Merge")
        dlg.setModal(True)
        dlg.setMinimumSize(640, 420)
        dlg.setStyleSheet(
            "QDialog { background: #20252e; color: #e8ecf1; }"
            "QLabel { color: #dbe3ee; }"
            "QListWidget { background: #151a21; color: #f4f7fb; border: 1px solid #3b4557; border-radius: 8px; }"
            "QListWidget::item { height: 30px; }"
            "QLineEdit { min-height: 34px; }"
            "QPushButton { padding: 8px 14px; border-radius: 8px; border: 1px solid #3b4557; background: #2b3340; color: #f4f7fb; }"
            "QPushButton:hover { background: #364255; }"
        )

        layout = QVBoxLayout(dlg)
        info_label = QLabel(
            f"School: {school_name}\n"
            f"Current folder: {self._strip_upcoming_prefix_for_display(current_folder)}\n\n"
            "Select ONE target folder.\n"
            "Result name = target folder base + PID.\n"
            "Close window (X) to return."
        )
        layout.addWidget(info_label)

        search_input = QLineEdit()
        search_input.setPlaceholderText("Search target folder...")
        layout.addWidget(search_input)

        lw = QListWidget()
        lw.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        all_candidates = list(candidates)

        def _refresh_candidates() -> None:
            needle = (search_input.text() or "").strip().lower()
            lw.clear()
            for name in all_candidates:
                display = self._strip_upcoming_prefix_for_display(name)
                if needle and needle not in display.lower():
                    continue
                item = QListWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, name)
                lw.addItem(item)

        _refresh_candidates()
        search_input.textChanged.connect(lambda _text: _refresh_candidates())
        layout.addWidget(lw)

        row = QHBoxLayout()
        btn_merge = QPushButton("Merge Selected")
        row.addWidget(btn_merge)
        layout.addLayout(row)

        result = {"target": None}

        def _confirm():
            items = lw.selectedItems()
            if len(items) != 1:
                self._show_message_box_topmost_non_modal(
                    QMessageBox.Icon.Warning,
                    "Select One",
                    "Please select exactly one target folder.",
                    parent=dlg,
                )
                return
            result["target"] = items[0].data(Qt.ItemDataRole.UserRole) or items[0].text()
            dlg.accept()

        btn_merge.clicked.connect(_confirm)
        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[info_label, search_input, lw],
            button_widgets=[btn_merge],
            base_width=900,
        )

        code = self._show_dialog_topmost_non_modal(dlg)
        if code != QDialog.DialogCode.Accepted:
            # Closing via X returns to previous cancellation action dialog.
            return None
        return result["target"]

    def _run_cancellation_in_ui(self) -> bool:
        report = self._run_cancellation_report_subprocess()
        missing_rows = report.get("missing_rows", []) or []
        _append_ui_runtime_log(f"cancellation ui start rows={len(missing_rows)}")
        if not missing_rows:
            _append_ui_runtime_log("cancellation ui no missing rows")
            self._show_info_message_dialog(
                "Cancellation Check",
                "No missing folder-event matches found.",
                min_width=420,
                min_height=190,
            )
            return False

        cal_mod = self._calendar_module()
        selectable_folder_map = {
            row["folder_name"]: row
            for row in (report.get("folder_rows", []) or [])
        }
        selectable_folder_names = self._ordered_reschedule_candidates(selectable_folder_map)

        actions_taken = []
        db_changed = False
        ignored_reason_codes: set[str] = set()
        reason_dialog_history: list[int] = []
        force_show_idx: int | None = None
        locked_rows_next_only: dict[int, bool] = {}
        applied_action_by_row: dict[int, dict] = {}
        undo_seq = 0

        def _row_entry(name: str) -> dict:
            return {
                "folder_name": name,
                "folder_path": os.path.join(self.base_dir, name),
                "source_folder_path": os.path.join(self.base_dir, name),
            }

        def _snapshot_item(item):
            if not item:
                return None
            return {
                "id": int(item.id),
                "disk_name": str(item.disk_name),
                "display_name": str(item.display_name or item.disk_name),
                "stage": int(item.stage),
                "flag_i": bool(item.flag_i),
                "flag_g": bool(item.flag_g),
                "in_progress_by": item.in_progress_by,
                "pid": item.pid,
                "note": item.note,
                "action_note": getattr(item, "action_note", None),
                "contact_name": getattr(item, "contact_name", None),
                "contact_email": getattr(item, "contact_email", None),
                "contact_phone": getattr(item, "contact_phone", None),
                "note_color": item.note_color,
                "shoot_date": item.shoot_date,
                "pdf_path": item.pdf_path,
                "pdf_path_2": getattr(item, "pdf_path_2", None),
                "pdf_path_3": getattr(item, "pdf_path_3", None),
                "pdf_path_4": getattr(item, "pdf_path_4", None),
                "late_pdf_path": getattr(item, "late_pdf_path", None),
                "excel_path": item.excel_path,
                "orders_form_path": item.orders_form_path,
                "orders_form_path_2": getattr(item, "orders_form_path_2", None),
                "orders_form_path_3": getattr(item, "orders_form_path_3", None),
                "orders_form_path_4": getattr(item, "orders_form_path_4", None),
                "qr_roster_path": item.qr_roster_path,
                "qr_orders_path": item.qr_orders_path,
                "workflow_domain": getattr(item, "workflow_domain", WORKFLOW_DOMAIN_PREPAID),
                "workflow_step": getattr(item, "workflow_step", None),
            }

        def _restore_item_snapshot(snapshot: dict | None, *, disk_name_override: str | None = None) -> str | None:
            if not snapshot:
                return None
            return self.db.restore_item_snapshot(snapshot, disk_name_override=disk_name_override)

        def _remove_path(path: str) -> None:
            if not path:
                return
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)

        def _ask_undo_selected_action(action_label: str) -> bool:
            answer = self._ask_question_topmost_non_modal(
                "Undo Selected Action",
                (
                    f"Previous row already applied '{action_label}'.\n\n"
                    "Undo this action and return to all options?\n"
                    "Yes = Undo and show all options.\n"
                    "No = Keep action and show Next only."
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
                button_labels={
                    int(QMessageBox.Yes): "Yes (Undo)",
                    int(QMessageBox.No): "No (Next Only)",
                },
            )
            return answer == int(QMessageBox.Yes)

        def _undo_applied_action(payload: dict) -> tuple[bool, str]:
            nonlocal selectable_folder_names
            kind = str(payload.get("kind") or "")
            if kind == "cancel":
                folder_name = str(payload.get("folder_name") or "")
                moved_path = str(payload.get("moved_path") or "")
                restored_name = folder_name

                try:
                    def _undo_cancel_fs() -> str | None:
                        if not moved_path or not os.path.isdir(moved_path):
                            return None
                        original_target = os.path.join(self.base_dir, folder_name)
                        if os.path.exists(original_target):
                            restored_target = self._build_unique_path(self.base_dir, folder_name)
                        else:
                            restored_target = original_target
                        self._run_fs_with_retry(
                            f"undo cancel move {moved_path} -> {restored_target}",
                            lambda: shutil.move(moved_path, restored_target),
                        )
                        return restored_target

                    restored_target = self._run_blocking_io_task(
                        "Undo Cancellation",
                        f"Restoring cancelled folder:\n{folder_name}",
                        _undo_cancel_fs,
                    )
                    if restored_target:
                        restored_name = os.path.basename(restored_target)
                    restored_from_db = _restore_item_snapshot(payload.get("db_snapshot"), disk_name_override=restored_name)
                    restored_name = restored_from_db or restored_name
                    if restored_name:
                        selectable_folder_map[restored_name] = _row_entry(restored_name)
                    selectable_folder_names = self._ordered_reschedule_candidates(selectable_folder_map)
                    return True, f"Undo cancel complete: {folder_name}"
                except Exception as exc:
                    return False, f"Undo cancel failed: {exc}"

            if kind == "reschedule":
                source_name = str(payload.get("source_name") or "")
                target_name = str(payload.get("target_name") or "")
                final_name = str(payload.get("final_name") or target_name)
                backup_source = str(payload.get("backup_source") or "")
                backup_target = str(payload.get("backup_target") or "")
                try:
                    def _undo_reschedule_fs() -> None:
                        _remove_path(os.path.join(self.base_dir, final_name))
                        _remove_path(os.path.join(self.base_dir, source_name))
                        _remove_path(os.path.join(self.base_dir, target_name))

                        if backup_target and os.path.isdir(backup_target):
                            self._run_fs_with_retry(
                                f"undo reschedule restore target {backup_target}",
                                lambda: shutil.copytree(backup_target, os.path.join(self.base_dir, target_name)),
                            )
                        if backup_source and os.path.isdir(backup_source):
                            self._run_fs_with_retry(
                                f"undo reschedule restore source {backup_source}",
                                lambda: shutil.copytree(backup_source, os.path.join(self.base_dir, source_name)),
                            )

                    self._run_blocking_io_task(
                        "Undo Reschedule",
                        f"Restoring folders for:\n{source_name}",
                        _undo_reschedule_fs,
                    )

                    _restore_item_snapshot(payload.get("target_snapshot"), disk_name_override=target_name)
                    _restore_item_snapshot(payload.get("source_snapshot"), disk_name_override=source_name)

                    selectable_folder_map.pop(final_name, None)
                    if target_name:
                        selectable_folder_map[target_name] = _row_entry(target_name)
                    if source_name:
                        selectable_folder_map[source_name] = _row_entry(source_name)
                    selectable_folder_names = self._ordered_reschedule_candidates(selectable_folder_map)
                    return True, f"Undo reschedule complete: {source_name}"
                except Exception as exc:
                    return False, f"Undo reschedule failed: {exc}"

            return False, "No undo payload found."

        idx = 0
        while idx < len(missing_rows):
            self._yield_ui()
            row = missing_rows[idx]
            folder_name = row["folder_name"]
            school_name = row["school_name"]
            yymmdd = row["yymmdd"]
            reason_text = row.get("reason_text") or "No matching Calendar event found"
            reason_code = row.get("reason_code") or "no_matching_calendar_event"
            is_forced_from_previous = (force_show_idx == idx)

            if reason_code in ignored_reason_codes and force_show_idx != idx:
                idx += 1
                continue
            force_show_idx = None

            jump_previous = False
            row_locked_next_only = bool(locked_rows_next_only.get(idx, False))
            while True:
                self._yield_ui()
                if not reason_dialog_history or reason_dialog_history[-1] != idx:
                    reason_dialog_history.append(idx)
                action = self._ask_missing_action_qt(
                    folder_name,
                    school_name,
                    yymmdd,
                    self.base_dir,
                    reason_text=reason_text,
                    next_only=row_locked_next_only,
                )
                if action == "next":
                    idx += 1
                    break
                if action == "previous":
                    if len(reason_dialog_history) <= 1:
                        self._show_info_message_dialog(
                            "Cancellation Check",
                            "Already at the first item.",
                            min_width=400,
                            min_height=180,
                        )
                        continue

                    _ = reason_dialog_history.pop()
                    prev_idx = reason_dialog_history.pop()
                    prev_payload = applied_action_by_row.get(prev_idx)

                    if prev_payload and not bool(locked_rows_next_only.get(prev_idx, False)):
                        should_undo = _ask_undo_selected_action(str(prev_payload.get("label") or "Action"))
                        if should_undo:
                            ok, msg = _undo_applied_action(prev_payload)
                            if ok:
                                actions_taken.append(msg)
                                applied_action_by_row.pop(prev_idx, None)
                                locked_rows_next_only[prev_idx] = False
                                db_changed = True
                            else:
                                self._show_message_box_topmost_non_modal(
                                    QMessageBox.Icon.Warning,
                                    "Undo Failed",
                                    msg,
                                )
                                # Keep current row active if undo failed.
                                reason_dialog_history.append(prev_idx)
                                reason_dialog_history.append(idx)
                                continue
                        else:
                            locked_rows_next_only[prev_idx] = True

                    idx = prev_idx
                    force_show_idx = prev_idx
                    jump_previous = True
                    break

                if action == "ignore":
                    if is_forced_from_previous and reason_code in ignored_reason_codes:
                        ignored_reason_codes.discard(reason_code)
                    locked_rows_next_only[idx] = False
                    applied_action_by_row.pop(idx, None)
                    idx += 1
                    break

                if action == "ignore_all":
                    ignored_reason_codes.add(reason_code)
                    locked_rows_next_only[idx] = False
                    applied_action_by_row.pop(idx, None)
                    idx += 1
                    break

                if action == "cancel":
                    confirm_cancel = self._confirm_dangerous_action(
                        title="Confirm Cancellation",
                        happened=(
                            f"Cancel this event?\n\nFolder: {folder_name}\nSchool: {school_name}\nDate: {yymmdd}"
                        ),
                        impacts=[
                            "The folder will be moved into the cancel folder.",
                            "The workflow DB row for this folder will be removed.",
                        ],
                        confirm_label="Cancel Event",
                        cancel_label="Keep Event",
                    )
                    if not confirm_cancel:
                        continue
                    try:
                        source_item = self.db.get_item_by_disk_name(folder_name)
                        moved_path = self._run_blocking_io_task(
                            "Cancel Event",
                            f"Moving folder to cancel:\n{folder_name}",
                            lambda: self._cancel_folder(folder_name),
                        )
                        self._delete_db_item_by_disk_name(folder_name)
                        if moved_path:
                            actions_taken.append(f"Cancelled: {folder_name} -> {moved_path}")
                        else:
                            actions_taken.append(f"Cancelled (DB removed, folder not found): {folder_name}")
                        db_changed = True
                        locked_rows_next_only[idx] = False
                        applied_action_by_row[idx] = {
                            "kind": "cancel",
                            "label": "Cancel Event",
                            "folder_name": folder_name,
                            "moved_path": moved_path,
                            "db_snapshot": _snapshot_item(source_item),
                        }
                        selectable_folder_map.pop(folder_name, None)
                        selectable_folder_names = self._ordered_reschedule_candidates(selectable_folder_map)
                    except Exception as e:
                        _append_ui_runtime_log(f"cancel action failed for {folder_name}: {e}")
                        actions_taken.append(f"Cancel DB failed: {folder_name} ({e})")
                    idx += 1
                    break

                if len(selectable_folder_names) < 2:
                    idx += 1
                    break

                target_name = self._ask_reschedule_target_qt(school_name, folder_name, selectable_folder_names)
                if not target_name:
                    continue

                current_row = selectable_folder_map.get(folder_name)
                target_row = selectable_folder_map.get(target_name)
                if not current_row or not target_row:
                    actions_taken.append(f"Reschedule failed (folder not found): {folder_name}")
                    break

                try:
                    source_item = self.db.get_item_by_disk_name(folder_name)
                    target_item = self.db.get_item_by_disk_name(target_name)
                    if not source_item or not target_item:
                        actions_taken.append(f"Reschedule DB rows missing: {folder_name}")
                        break

                    undo_seq += 1
                    safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{folder_name}_{target_name}")[:80]
                    undo_dir = os.path.join(self.base_dir, "_workflow_undo", f"{undo_seq:04d}_{safe_key}")
                    backup_source = os.path.join(undo_dir, "source")
                    backup_target = os.path.join(undo_dir, "target")

                    merged_name_raw = cal_mod._build_merged_folder_name(target_name, folder_name) or target_name
                    merged_name = self.db.get_unique_disk_name(merged_name_raw, exclude_item_id=target_item.id)

                    def _reschedule_fs_task() -> str:
                        self._run_fs_with_retry(
                            f"ensure undo dir {undo_dir}",
                            lambda: os.makedirs(undo_dir, exist_ok=True),
                        )
                        source_path = self._ensure_folder_available(folder_name)
                        target_path = self._ensure_folder_available(target_name)

                        def _copy_source_backup() -> None:
                            if os.path.isdir(backup_source):
                                return
                            shutil.copytree(source_path, backup_source)

                        def _copy_target_backup() -> None:
                            if os.path.isdir(backup_target):
                                return
                            shutil.copytree(target_path, backup_target)

                        self._run_fs_with_retry(
                            f"backup source folder {source_path}",
                            _copy_source_backup,
                        )
                        self._run_fs_with_retry(
                            f"backup target folder {target_path}",
                            _copy_target_backup,
                        )

                        return self._merge_folders_in_base(
                            target_folder_name=target_name,
                            source_folder_name=folder_name,
                            desired_merged_name=merged_name,
                        )

                    final_folder_name = self._run_blocking_io_task(
                        "Reschedule Event",
                        f"Merging folder:\n{folder_name}\ninto\n{target_name}",
                        _reschedule_fs_task,
                    )

                    if final_folder_name != target_name:
                        self.db.update_disk_name(target_item.id, final_folder_name)

                    self.db.delete_item(source_item.id)
                    self.moved_item_ids.add(int(target_item.id))
                    actions_taken.append(
                        f"Rescheduled: {folder_name} -> {target_name} => {final_folder_name}"
                    )
                    db_changed = True
                    locked_rows_next_only[idx] = False
                    applied_action_by_row[idx] = {
                        "kind": "reschedule",
                        "label": "Reschedule Event",
                        "source_name": folder_name,
                        "target_name": target_name,
                        "final_name": final_folder_name,
                        "backup_source": backup_source,
                        "backup_target": backup_target,
                        "source_snapshot": _snapshot_item(source_item),
                        "target_snapshot": _snapshot_item(target_item),
                    }
                    selectable_folder_map.pop(folder_name, None)
                    selectable_folder_map.pop(target_name, None)
                    selectable_folder_map[final_folder_name] = _row_entry(final_folder_name)
                    selectable_folder_names = self._ordered_reschedule_candidates(selectable_folder_map)
                except Exception as e:
                    _append_ui_runtime_log(f"reschedule action failed for {folder_name}: {e}")
                    actions_taken.append(f"Reschedule DB merge failed: {folder_name} ({e})")
                idx += 1
                break

            if jump_previous:
                continue

        if actions_taken:
            completed_cancel: list[str] = []
            completed_reschedule: list[str] = []
            completed_undo: list[str] = []
            needs_attention: list[str] = []

            for raw in actions_taken:
                text = str(raw or "").strip()
                if not text:
                    continue
                if text.startswith("Cancelled: "):
                    body = text[len("Cancelled: ") :].strip()
                    folder_name = body.split(" -> ", 1)[0].strip()
                    completed_cancel.append(self._strip_upcoming_prefix_for_display(folder_name))
                    continue
                if text.startswith("Cancelled (DB removed, folder not found): "):
                    folder_name = text.split(":", 1)[1].strip()
                    completed_cancel.append(self._strip_upcoming_prefix_for_display(folder_name))
                    continue
                if text.startswith("Rescheduled: "):
                    body = text[len("Rescheduled: ") :].strip()
                    source_name = body
                    final_name = body
                    if " => " in body:
                        left, final_name = body.split(" => ", 1)
                        source_name = left.split(" -> ", 1)[0].strip()
                    completed_reschedule.append(
                        f"{self._strip_upcoming_prefix_for_display(source_name)} -> "
                        f"{self._strip_upcoming_prefix_for_display(final_name.strip())}"
                    )
                    continue
                if text.startswith("Undo cancel complete: "):
                    folder_name = text.split(":", 1)[1].strip()
                    completed_undo.append(
                        f"Undo cancellation: {self._strip_upcoming_prefix_for_display(folder_name)}"
                    )
                    continue
                if text.startswith("Undo reschedule complete: "):
                    folder_name = text.split(":", 1)[1].strip()
                    completed_undo.append(
                        f"Undo reschedule: {self._strip_upcoming_prefix_for_display(folder_name)}"
                    )
                    continue

                if text.startswith("Cancel DB failed: "):
                    body = text[len("Cancel DB failed: ") :].strip()
                    folder_name = body.split(" (", 1)[0].strip()
                    needs_attention.append(
                        f"Could not cancel: {self._strip_upcoming_prefix_for_display(folder_name)}"
                    )
                    continue
                if text.startswith("Reschedule failed (folder not found): "):
                    folder_name = text.split(":", 1)[1].strip()
                    needs_attention.append(
                        f"Could not reschedule: {self._strip_upcoming_prefix_for_display(folder_name)}"
                    )
                    continue
                if text.startswith("Reschedule DB rows missing: "):
                    folder_name = text.split(":", 1)[1].strip()
                    needs_attention.append(
                        f"Could not reschedule: {self._strip_upcoming_prefix_for_display(folder_name)}"
                    )
                    continue
                if text.startswith("Reschedule DB merge failed: "):
                    body = text[len("Reschedule DB merge failed: ") :].strip()
                    folder_name = body.split(" (", 1)[0].strip()
                    needs_attention.append(
                        f"Could not reschedule: {self._strip_upcoming_prefix_for_display(folder_name)}"
                    )
                    continue

                needs_attention.append(text)

            sections: list[str] = []
            completed_lines: list[str] = []
            if completed_cancel:
                completed_lines.append(f"Cancelled: {len(completed_cancel)}")
            if completed_reschedule:
                completed_lines.append(f"Rescheduled: {len(completed_reschedule)}")
            if completed_undo:
                completed_lines.append(f"Undo completed: {len(completed_undo)}")
            if completed_lines:
                sections.append("Completed\n" + "\n".join(f"- {line}" for line in completed_lines))

            detail_lines = (
                [f"Cancelled: {name}" for name in completed_cancel]
                + [f"Rescheduled: {name}" for name in completed_reschedule]
                + completed_undo
            )
            if detail_lines:
                preview = detail_lines[:300]
                details_text = "\n".join(f"- {line}" for line in preview)
                if len(detail_lines) > len(preview):
                    details_text += f"\n...and {len(detail_lines) - len(preview)} more"
                sections.append("Details\n" + details_text)

            if needs_attention:
                preview = needs_attention[:120]
                attention_text = "\n".join(f"- {line}" for line in preview)
                if len(needs_attention) > len(preview):
                    attention_text += f"\n...and {len(needs_attention) - len(preview)} more"
                sections.append("Needs Attention\n" + attention_text)

            self._show_info_text_dialog(
                "Actions Completed",
                "\n\n".join(sections) if sections else "No workflow changes were made.",
                min_width=760,
                min_height=420,
            )
        _append_ui_runtime_log(
            f"cancellation ui complete db_changed={db_changed} actions={len(actions_taken)}"
        )
        return db_changed

    def _strip_upcoming_prefix_for_display(self, name: str) -> str:
        text = (name or "").strip()
        if text.startswith("1. Upcoming"):
            return text[len("1. Upcoming"):].strip()
        if text.startswith("1.Upcoming"):
            return text[len("1.Upcoming"):].strip()
        return text

    def _ordered_reschedule_candidates(self, selectable_folder_map: dict[str, dict]) -> list[str]:
        available = set(selectable_folder_map.keys())
        ordered: list[str] = []

        # Keep same order as UI Upcoming column (DB stage=1 order).
        try:
            for it in self.db.list_by_stage(1):
                if it.disk_name in available and it.disk_name not in ordered:
                    ordered.append(it.disk_name)
        except Exception:
            pass

        # Append any in-session merged names not present in DB stage listing yet.
        remaining = [name for name in available if name not in ordered]
        remaining.sort(key=lambda s: self._strip_upcoming_prefix_for_display(s).lower())
        ordered.extend(remaining)
        return ordered

    def _build_unique_path(self, parent_dir: str, folder_name: str) -> str:
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

    def _yield_ui(self) -> None:
        app = QApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            app.processEvents()

    def _run_blocking_io_task(self, title: str, message: str, task):
        """
        Runs heavy filesystem task on a worker thread while keeping the UI responsive.
        Raises RuntimeError with traceback details on failure.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumWidth(420)
        dlg.setWindowFlag(Qt.WindowCloseButtonHint, False)

        layout = QVBoxLayout(dlg)
        label = QLabel(message)
        label.setWordWrap(True)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        layout.addWidget(label)
        layout.addWidget(bar)
        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[label],
            button_widgets=[],
            base_width=520,
        )

        worker = _IoTaskThread(task)
        worker.finished.connect(dlg.accept)
        worker.start()
        try:
            self._show_dialog_topmost_non_modal(dlg)
        finally:
            worker.wait()

        if worker.error is not None:
            tb = (worker.traceback_text or "").strip()
            msg = f"{worker.error}"
            if tb:
                msg = f"{msg}\n\n{tb}"
            raise RuntimeError(msg)
        return worker.result

    def _run_fs_with_retry(
        self,
        op_label: str,
        operation,
        *,
        attempts: int = 4,
        initial_delay_seconds: float = 0.12,
    ):
        last_error: Exception | None = None
        delay = max(0.01, float(initial_delay_seconds))
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                result = operation()
                if attempt > 1:
                    self._set_retry_status("")
                return result
            except (OSError, shutil.Error) as exc:
                last_error = exc
                _append_ui_runtime_log(
                    f"FS retry {attempt}/{attempts} failed for {op_label}: {exc}"
                )
                if attempt >= attempts:
                    self._set_retry_status("")
                    raise
                self._set_retry_status(
                    f"DAMY folder is busy or temporarily unavailable. Retrying ({attempt}/{attempts - 1})..."
                )
                app = QApplication.instance()
                if app is not None and QThread.currentThread() == app.thread():
                    self._yield_ui()
                time.sleep(delay)
                delay = min(1.0, delay * 2.0)
        if last_error:
            self._set_retry_status("")
            raise last_error

    def _source_folder_path(self, folder_name: str) -> str | None:
        source_dir = (self.source_base_dir or "").strip()
        if not source_dir:
            return None
        source_path = os.path.join(source_dir, folder_name)
        if os.path.isdir(source_path):
            return source_path
        source_cancel = os.path.join(source_dir, "cancel", folder_name)
        if os.path.isdir(source_cancel):
            return source_cancel
        return None

    def _ensure_folder_available(self, folder_name: str) -> str:
        target_path = os.path.join(self.base_dir, folder_name)
        if os.path.isdir(target_path):
            return target_path

        source_path = self._source_folder_path(folder_name)
        if source_path and os.path.isdir(source_path):
            self._run_fs_with_retry(
                f"ensure base dir {self.base_dir}",
                lambda: os.makedirs(self.base_dir, exist_ok=True),
            )

            def _copy_if_needed():
                if os.path.isdir(target_path):
                    return
                shutil.copytree(source_path, target_path)

            self._run_fs_with_retry(
                f"copytree {source_path} -> {target_path}",
                _copy_if_needed,
            )
            return target_path

        raise RuntimeError(f"Folder not found in active base or source base: {folder_name}")

    def _cancel_folder(self, folder_name: str) -> str | None:
        try:
            folder_path = self._ensure_folder_available(folder_name)
        except Exception:
            return None
        cancel_dir = os.path.join(self.base_dir, "cancel")
        self._run_fs_with_retry(
            f"ensure cancel dir {cancel_dir}",
            lambda: os.makedirs(cancel_dir, exist_ok=True),
        )
        dest_path = self._build_unique_path(cancel_dir, os.path.basename(folder_path))
        self._run_fs_with_retry(
            f"move cancel folder {folder_path} -> {dest_path}",
            lambda: shutil.move(folder_path, dest_path),
        )
        return dest_path

    def _move_item_with_merge(self, src_path: str, dst_dir: str) -> None:
        name = os.path.basename(src_path)
        dst_path = os.path.join(dst_dir, name)

        if not os.path.exists(dst_path):
            self._run_fs_with_retry(
                f"move merge item {src_path} -> {dst_path}",
                lambda: shutil.move(src_path, dst_path),
            )
            return

        if os.path.isdir(src_path) and os.path.isdir(dst_path):
            for child in os.listdir(src_path):
                self._move_item_with_merge(os.path.join(src_path, child), dst_path)
            if os.path.isdir(src_path):
                self._run_fs_with_retry(
                    f"rmtree merge source {src_path}",
                    lambda: shutil.rmtree(src_path, ignore_errors=True),
                )
            return

        base, ext = os.path.splitext(name)
        i = 1
        while True:
            candidate = os.path.join(dst_dir, f"{base} ({i}){ext}")
            if not os.path.exists(candidate):
                self._run_fs_with_retry(
                    f"move merge collision {src_path} -> {candidate}",
                    lambda: shutil.move(src_path, candidate),
                )
                return
            i += 1

    def _make_unique_folder_name_in_base(self, desired_name: str) -> str:
        if not os.path.exists(os.path.join(self.base_dir, desired_name)):
            return desired_name
        i = 1
        while True:
            candidate = f"{desired_name} ({i})"
            if not os.path.exists(os.path.join(self.base_dir, candidate)):
                return candidate
            i += 1

    def _merge_folders_in_base(
        self,
        target_folder_name: str,
        source_folder_name: str,
        desired_merged_name: str,
    ) -> str:
        target_path = self._ensure_folder_available(target_folder_name)
        source_path = self._ensure_folder_available(source_folder_name)

        if os.path.abspath(target_path) == os.path.abspath(source_path):
            raise RuntimeError("Cannot merge a folder into itself.")

        for child in os.listdir(source_path):
            self._move_item_with_merge(os.path.join(source_path, child), target_path)
        if os.path.isdir(source_path):
            self._run_fs_with_retry(
                f"rmtree merged source {source_path}",
                lambda: shutil.rmtree(source_path, ignore_errors=True),
            )

        final_name = self._make_unique_folder_name_in_base(desired_merged_name)
        final_path = os.path.join(self.base_dir, final_name)
        if os.path.abspath(final_path) != os.path.abspath(target_path):
            self._run_fs_with_retry(
                f"rename merged folder {target_path} -> {final_path}",
                lambda: shutil.move(target_path, final_path),
            )
        return final_name

    def _wire_draglistwidget_db(self, lw: DragListWidget, stage: int):
        lw.set_db(self.db)
        lw.set_stage(stage)
        lw.set_current_user(self.current_user)

    def _stage_supports_folder_assets(self, stage: int) -> bool:
        # Folder Assets UI is intentionally limited to stage 1 only.
        try:
            return int(stage) == 1
        except Exception:
            return False

    def _wire_folder_action_popup(self, lw: DragListWidget) -> None:
        lw.itemClicked.connect(lambda item, source=lw: self._show_folder_action_popup_for_item(source, item))
        lw.itemSelectionChanged.connect(lambda source=lw: self._on_folder_action_selection_changed(source))

    def _iter_folder_list_widgets(self) -> list[QListWidget]:
        widgets: list[QListWidget] = []
        seen: set[int] = set()
        for _column, lw in self.column_widgets:
            if id(lw) not in seen:
                widgets.append(lw)
                seen.add(id(lw))
        if self.edit_widgets:
            for lw in self.edit_widgets:
                if id(lw) not in seen:
                    widgets.append(lw)
                    seen.add(id(lw))
        return widgets

    def _clear_folder_list_selections(self, *, except_widget: QListWidget | None = None) -> None:
        for lw in self._iter_folder_list_widgets():
            if except_widget is not None and lw is except_widget:
                continue
            try:
                lw.clearSelection()
                lw.setCurrentRow(-1)
            except Exception:
                continue

    def _ensure_folder_action_popup(self) -> None:
        if self.folder_action_popup is not None:
            return

        panel = QFrame()
        panel.setObjectName("folderActionPopup")
        panel.setFrameShape(QFrame.Shape.NoFrame)
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        panel.setStyleSheet(
            "QFrame#folderActionPopup {"
            " background: #1c2430; border: 1px solid #3c4d64; border-radius: 12px; }"
            "QPushButton { min-height: 42px; padding: 6px 12px; border-radius: 10px; "
            "border: 1px solid #3d5470; background: #263344; color: #f4f7fb; text-align: left; }"
            "QPushButton:hover { background: #2f4158; }"
            "QPushButton#assetClearButton {"
            " min-height: 18px; max-height: 18px; min-width: 18px; max-width: 18px;"
            " margin: 0; padding: 0 0 1px 0; border-radius: 9px;"
            " border: 1px solid #cf6a6a; background: #a84444;"
            " color: #fff6f6; font-size: 10px; font-weight: 700; text-align: center; }"
            "QPushButton#assetClearButton:hover { background: #c45454; border-color: #e08383; }"
            "QPushButton#assetClearButton:pressed { background: #8f3838; }"
            "QPushButton#assetAddButton {"
            " min-height: 26px; max-height: 26px; min-width: 26px; max-width: 26px;"
            " padding: 0; border-radius: 13px; border: 1px solid #5b7ca3;"
            " background: #2a4564; color: #eaf4ff; font-size: 16px; font-weight: 700; text-align: center; }"
            "QPushButton#assetAddButton:hover { background: #35608c; border-color: #78a7d9; }"
            "QPushButton#assetAddButton:pressed { background: #23445f; }"
            "QLabel#folderActionTitle { color: #f5f8fc; font-size: 14px; font-weight: 700; }"
            "QLabel#folderActionHint { color: #9fb0c4; font-size: 12px; }"
            "QLabel#folderActionInlineWarning {"
            " color: #ffd59b; background: #3a2d1b; border: 1px solid #8f6a32; border-radius: 8px;"
            " padding: 8px 10px; font-size: 11px; }"
        )

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Folder Assets")
        title.setObjectName("folderActionTitle")
        hint = QLabel(FOLDER_ASSET_HINT_TEXT)
        hint.setObjectName("folderActionHint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        orders_row = QHBoxLayout()
        orders_row.setContentsMargins(0, 0, 0, 0)
        orders_row.setSpacing(8)
        orders_form_zone = AssetDropZone("Make Orders Form")
        orders_form_zone_2 = AssetDropZone("Make Orders Form #2")
        orders_form_zone_3 = AssetDropZone("Make Orders Form #3")
        orders_form_zone_4 = AssetDropZone("Make Orders Form #4")
        orders_form_zone_2.setVisible(False)
        orders_form_zone_3.setVisible(False)
        orders_form_zone_4.setVisible(False)
        orders_form_zone.setMinimumWidth(0)
        orders_form_zone.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        orders_form_zone_2.setMinimumWidth(0)
        orders_form_zone_2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        orders_form_zone_3.setMinimumWidth(0)
        orders_form_zone_3.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        orders_form_zone_4.setMinimumWidth(0)
        orders_form_zone_4.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        orders_form_add_button = QPushButton("+")
        orders_form_add_button.setObjectName("assetAddButton")
        orders_form_add_button.setToolTip("Add next Make Orders Form slot")
        orders_form_add_button.setCursor(Qt.PointingHandCursor)
        orders_row.addWidget(orders_form_zone)
        orders_row.addWidget(orders_form_zone_2)
        orders_row.addWidget(orders_form_zone_3)
        orders_row.addWidget(orders_form_zone_4)
        orders_row.addWidget(orders_form_add_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(orders_row)

        assets_row = QHBoxLayout()
        assets_row.setContentsMargins(0, 0, 0, 0)
        assets_row.setSpacing(8)
        pdf_zone = AssetDropZone("PDF")
        pdf_zone_2 = AssetDropZone("PDF #2")
        pdf_zone_3 = AssetDropZone("PDF #3")
        pdf_zone_4 = AssetDropZone("PDF #4")
        late_pdf_zone = AssetDropZone("Late PDF")
        pdf_zone_2.setVisible(False)
        pdf_zone_3.setVisible(False)
        pdf_zone_4.setVisible(False)
        late_pdf_zone.setVisible(False)
        excel_zone = AssetDropZone("Excel")
        pdf_zone.setMinimumWidth(0)
        pdf_zone_2.setMinimumWidth(0)
        pdf_zone_3.setMinimumWidth(0)
        pdf_zone_4.setMinimumWidth(0)
        late_pdf_zone.setMinimumWidth(0)
        excel_zone.setMinimumWidth(0)
        pdf_zone.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        pdf_zone_2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        pdf_zone_3.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        pdf_zone_4.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        late_pdf_zone.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        excel_zone.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        pdf_add_button = QPushButton("+")
        pdf_add_button.setObjectName("assetAddButton")
        pdf_add_button.setToolTip("Add next PDF slot")
        pdf_add_button.setCursor(Qt.PointingHandCursor)
        pdf_group = QWidget()
        pdf_group_layout = QHBoxLayout(pdf_group)
        pdf_group_layout.setContentsMargins(0, 0, 0, 0)
        pdf_group_layout.setSpacing(8)
        pdf_group_layout.addWidget(pdf_zone, 1)
        pdf_group_layout.addWidget(pdf_zone_2, 1)
        pdf_group_layout.addWidget(pdf_zone_3, 1)
        pdf_group_layout.addWidget(pdf_zone_4, 1)
        pdf_group_layout.addWidget(late_pdf_zone, 1)
        pdf_group_layout.addWidget(pdf_add_button, 0, Qt.AlignmentFlag.AlignVCenter)
        assets_row.addWidget(pdf_group, 1)
        assets_row.addWidget(excel_zone, 1)
        layout.addLayout(assets_row)

        qr_row = QHBoxLayout()
        qr_row.setContentsMargins(0, 0, 0, 0)
        qr_row.setSpacing(8)
        qr_roster_zone = AssetDropZone("QR Roster")
        qr_orders_zone = AssetDropZone("QR Orders")
        qr_row.addWidget(qr_roster_zone)
        qr_row.addWidget(qr_orders_zone)
        layout.addLayout(qr_row)

        inline_warning = QLabel("")
        inline_warning.setObjectName("folderActionInlineWarning")
        inline_warning.setWordWrap(True)
        inline_warning.setVisible(False)
        layout.addWidget(inline_warning)

        orders_form_zone.clicked.connect(self._on_folder_action_orders_form_clicked)
        orders_form_zone.doubleClicked.connect(self._on_folder_action_orders_form_open_requested)
        orders_form_zone.clearRequested.connect(lambda: self._clear_asset_for_current_item("orders_form"))
        orders_form_zone.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("orders_form", path))
        orders_form_zone_2.clicked.connect(self._on_folder_action_orders_form_2_clicked)
        orders_form_zone_2.doubleClicked.connect(self._on_folder_action_orders_form_2_open_requested)
        orders_form_zone_2.clearRequested.connect(lambda: self._clear_asset_for_current_item("orders_form_2"))
        orders_form_zone_2.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("orders_form_2", path))
        orders_form_zone_3.clicked.connect(self._on_folder_action_orders_form_3_clicked)
        orders_form_zone_3.doubleClicked.connect(self._on_folder_action_orders_form_3_open_requested)
        orders_form_zone_3.clearRequested.connect(lambda: self._clear_asset_for_current_item("orders_form_3"))
        orders_form_zone_3.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("orders_form_3", path))
        orders_form_zone_4.clicked.connect(self._on_folder_action_orders_form_4_clicked)
        orders_form_zone_4.doubleClicked.connect(self._on_folder_action_orders_form_4_open_requested)
        orders_form_zone_4.clearRequested.connect(lambda: self._clear_asset_for_current_item("orders_form_4"))
        orders_form_zone_4.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("orders_form_4", path))
        orders_form_add_button.clicked.connect(lambda: self._on_folder_action_expand_second_card("orders_form"))
        excel_zone.clicked.connect(self._on_folder_action_excel_clicked)
        excel_zone.doubleClicked.connect(self._on_folder_action_excel_open_requested)
        excel_zone.clearRequested.connect(lambda: self._clear_asset_for_current_item("excel"))
        pdf_zone.clicked.connect(self._on_folder_action_pdf_clicked)
        pdf_zone.doubleClicked.connect(self._on_folder_action_pdf_open_requested)
        pdf_zone.clearRequested.connect(lambda: self._clear_asset_for_current_item("pdf"))
        pdf_zone.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("pdf", path))
        pdf_zone_2.clicked.connect(self._on_folder_action_pdf_2_clicked)
        pdf_zone_2.doubleClicked.connect(self._on_folder_action_pdf_2_open_requested)
        pdf_zone_2.clearRequested.connect(lambda: self._clear_asset_for_current_item("pdf_2"))
        pdf_zone_2.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("pdf_2", path))
        pdf_zone_3.clicked.connect(self._on_folder_action_pdf_3_clicked)
        pdf_zone_3.doubleClicked.connect(self._on_folder_action_pdf_3_open_requested)
        pdf_zone_3.clearRequested.connect(lambda: self._clear_asset_for_current_item("pdf_3"))
        pdf_zone_3.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("pdf_3", path))
        pdf_zone_4.clicked.connect(self._on_folder_action_pdf_4_clicked)
        pdf_zone_4.doubleClicked.connect(self._on_folder_action_pdf_4_open_requested)
        pdf_zone_4.clearRequested.connect(lambda: self._clear_asset_for_current_item("pdf_4"))
        pdf_zone_4.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("pdf_4", path))
        late_pdf_zone.clicked.connect(self._on_folder_action_late_pdf_clicked)
        late_pdf_zone.doubleClicked.connect(self._on_folder_action_late_pdf_open_requested)
        late_pdf_zone.clearRequested.connect(lambda: self._clear_asset_for_current_item("late_pdf"))
        late_pdf_zone.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("late_pdf", path))
        pdf_add_button.clicked.connect(lambda: self._on_folder_action_expand_second_card("pdf"))
        excel_zone.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("excel", path))
        qr_roster_zone.clicked.connect(self._on_folder_action_qr_roster_clicked)
        qr_roster_zone.doubleClicked.connect(self._on_folder_action_qr_roster_clicked)
        qr_roster_zone.clearRequested.connect(lambda: self._clear_asset_for_current_item("qr_roster"))
        qr_roster_zone.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("qr_roster", path))
        qr_orders_zone.clicked.connect(self._on_folder_action_qr_orders_clicked)
        qr_orders_zone.doubleClicked.connect(self._on_folder_action_qr_orders_clicked)
        qr_orders_zone.clearRequested.connect(lambda: self._clear_asset_for_current_item("qr_orders"))
        qr_orders_zone.fileDropped.connect(lambda path: self._on_folder_action_asset_dropped("qr_orders", path))

        self.folder_action_title_label = title
        self.folder_action_hint_label = hint
        self.folder_action_make_orders_zone = orders_form_zone
        self.folder_action_make_orders_zone_2 = orders_form_zone_2
        self.folder_action_make_orders_zone_3 = orders_form_zone_3
        self.folder_action_make_orders_zone_4 = orders_form_zone_4
        self.folder_action_make_orders_add_button = orders_form_add_button
        self.folder_action_pdf_zone = pdf_zone
        self.folder_action_pdf_zone_2 = pdf_zone_2
        self.folder_action_pdf_zone_3 = pdf_zone_3
        self.folder_action_pdf_zone_4 = pdf_zone_4
        self.folder_action_late_pdf_zone = late_pdf_zone
        self.folder_action_pdf_add_button = pdf_add_button
        self.folder_action_excel_zone = excel_zone
        self.folder_action_qr_roster_zone = qr_roster_zone
        self.folder_action_qr_orders_zone = qr_orders_zone
        self.folder_action_inline_warning_label = inline_warning
        self._refresh_optional_asset_cards_visibility()
        panel.hide()
        self.folder_action_popup = panel

    def _capture_folder_action_scroll_anchor(self, lw: DragListWidget | None) -> tuple[DragListWidget, int, int] | None:
        if lw is None:
            return None
        popup_item = self.folder_action_popup_host_item
        for row in range(lw.count()):
            item = lw.item(row)
            if item is None or item is popup_item:
                continue
            rect = lw.visualItemRect(item)
            if not rect.isValid() or rect.bottom() < 0:
                continue
            raw_id = item.data(ROLE_DB_ID)
            try:
                item_id = int(raw_id)
            except Exception:
                continue
            return (lw, item_id, rect.top())
        return None

    def _restore_folder_action_scroll_anchor(self, anchor: tuple[DragListWidget, int, int] | None) -> None:
        if anchor is None:
            return
        lw, item_id, expected_top = anchor
        item = self._find_item_by_id_in_list(lw, item_id)
        if item is None:
            return
        rect = lw.visualItemRect(item)
        if not rect.isValid():
            return
        delta = rect.top() - expected_top
        if delta:
            lw.verticalScrollBar().setValue(lw.verticalScrollBar().value() + delta)

    def _clear_folder_action_popup_host(self, *, preserve_scroll_anchor: bool = True) -> None:
        host_list = self.folder_action_popup_host_list
        host_item = self.folder_action_popup_host_item
        host_widget = self.folder_action_popup_host_widget
        panel = self.folder_action_popup
        scroll_anchor = self._capture_folder_action_scroll_anchor(host_list) if preserve_scroll_anchor else None

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

        self.folder_action_popup_host_list = None
        self.folder_action_popup_host_item = None
        self.folder_action_popup_host_widget = None
        if preserve_scroll_anchor:
            self._restore_folder_action_scroll_anchor(scroll_anchor)

    def _resize_folder_action_popup_host(self) -> None:
        host_list = self.folder_action_popup_host_list
        host_item = self.folder_action_popup_host_item
        host_widget = self.folder_action_popup_host_widget
        panel = self.folder_action_popup
        if host_list is None or host_item is None or host_widget is None or panel is None:
            return

        available_width = max(320, host_list.viewport().width() - 24)
        panel.setMaximumWidth(available_width)
        panel.adjustSize()
        host_widget.adjustSize()
        host_item.setSizeHint(QSize(max(0, host_list.viewport().width() - 4), host_widget.sizeHint().height()))

    def _on_folder_action_selection_changed(self, lw: DragListWidget) -> None:
        if self.folder_action_anchor_list is not lw:
            return
        if lw.selectedItems():
            return
        panel = self.folder_action_popup
        host_widget = self.folder_action_popup_host_widget
        if (
            panel is not None
            and panel.isVisible()
            and host_widget is not None
            and host_widget.isVisible()
        ):
            # Keep assets panel open when list selection is transiently cleared
            # by clicks inside the embedded assets host area.
            return
        self._hide_folder_action_popup()

    def _show_folder_action_popup_for_item(self, lw: DragListWidget, item: QListWidgetItem) -> None:
        if item is None:
            self._hide_folder_action_popup()
            return

        raw_item_id = item.data(ROLE_DB_ID)
        if raw_item_id is None:
            self._hide_folder_action_popup()
            return

        try:
            item_id = int(raw_item_id)
        except Exception:
            self._hide_folder_action_popup()
            return

        disk_name = str(item.data(ROLE_DISK_NAME) or item.text() or "").strip()
        if not disk_name:
            self._hide_folder_action_popup()
            return

        panel = self.folder_action_popup
        if (
            panel is not None
            and panel.isVisible()
            and self.folder_action_anchor_list is lw
            and self.folder_action_anchor_item_id == item_id
        ):
            # Keep original behavior: same row click toggles popup closed.
            self._hide_folder_action_popup(clear_selection=True)
            return

        self._ensure_folder_action_popup()
        panel = self.folder_action_popup
        if panel is None:
            return

        scroll_anchor = self._capture_folder_action_scroll_anchor(lw)
        self._clear_folder_list_selections(except_widget=lw)
        try:
            item.setSelected(True)
            lw.setCurrentItem(item)
        except Exception:
            pass

        self.folder_action_anchor_list = lw
        self.folder_action_anchor_item_id = item_id
        self.folder_action_anchor_db_item = None
        self.folder_action_anchor_disk_name = disk_name
        self._folder_action_show_orders_form_slot2 = False
        self._folder_action_show_orders_form_slot3 = False
        self._folder_action_show_orders_form_slot4 = False
        self._folder_action_show_pdf_slot2 = False
        self._folder_action_show_pdf_slot3 = False
        self._folder_action_show_pdf_slot4 = False
        self._folder_action_show_late_pdf = False
        title_label = getattr(self, "folder_action_title_label", None)
        hint_label = getattr(self, "folder_action_hint_label", None)

        if title_label is not None:
            title_label.setText(f"Folder Assets\n{disk_name}")
        if hint_label is not None:
            hint_label.setText(FOLDER_ASSET_HINT_TEXT)
        self._set_folder_action_inline_warning("")
        self._refresh_folder_action_asset_states(item_id)
        self._clear_folder_action_popup_host(preserve_scroll_anchor=False)

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
        lw.insertItem(lw.row(item) + 1, popup_item)
        lw.setItemWidget(popup_item, host_widget)

        self.folder_action_popup_host_list = lw
        self.folder_action_popup_host_item = popup_item
        self.folder_action_popup_host_widget = host_widget
        self._resize_folder_action_popup_host()
        self._restore_folder_action_scroll_anchor(scroll_anchor)

    def _hide_folder_action_popup(self, *, clear_selection: bool = False) -> None:
        anchor_list = self.folder_action_anchor_list
        self._clear_folder_action_popup_host()
        if self.folder_action_popup is not None:
            self.folder_action_popup.hide()
        self.folder_action_anchor_list = None
        self.folder_action_anchor_item_id = None
        self.folder_action_anchor_db_item = None
        self.folder_action_anchor_disk_name = ""
        self._folder_action_show_orders_form_slot2 = False
        self._folder_action_show_orders_form_slot3 = False
        self._folder_action_show_orders_form_slot4 = False
        self._folder_action_show_pdf_slot2 = False
        self._folder_action_show_pdf_slot3 = False
        self._folder_action_show_pdf_slot4 = False
        self._folder_action_show_late_pdf = False
        self._refresh_optional_asset_cards_visibility()
        if clear_selection:
            self._clear_folder_list_selections(except_widget=None if anchor_list is None else anchor_list)
            if anchor_list is not None:
                try:
                    anchor_list.clearSelection()
                    anchor_list.setCurrentRow(-1)
                except Exception:
                    pass

    def _set_folder_action_inline_warning(self, text: str = "") -> None:
        label = self.folder_action_inline_warning_label
        if label is None:
            return
        message = str(text or "").strip()
        label.setText(message)
        label.setVisible(bool(message))

    def _refresh_optional_asset_cards_visibility(self) -> None:
        orders_form_zone_2 = getattr(self, "folder_action_make_orders_zone_2", None)
        orders_form_zone_3 = getattr(self, "folder_action_make_orders_zone_3", None)
        orders_form_zone_4 = getattr(self, "folder_action_make_orders_zone_4", None)
        orders_form_add_button = getattr(self, "folder_action_make_orders_add_button", None)
        pdf_zone_2 = getattr(self, "folder_action_pdf_zone_2", None)
        pdf_zone_3 = getattr(self, "folder_action_pdf_zone_3", None)
        pdf_zone_4 = getattr(self, "folder_action_pdf_zone_4", None)
        late_pdf_zone = getattr(self, "folder_action_late_pdf_zone", None)
        pdf_add_button = getattr(self, "folder_action_pdf_add_button", None)

        show_orders_form_slot2 = bool(self._folder_action_show_orders_form_slot2)
        show_orders_form_slot3 = bool(self._folder_action_show_orders_form_slot3)
        show_orders_form_slot4 = bool(self._folder_action_show_orders_form_slot4)
        show_pdf_slot2 = bool(self._folder_action_show_pdf_slot2)
        show_pdf_slot3 = bool(self._folder_action_show_pdf_slot3)
        show_pdf_slot4 = bool(self._folder_action_show_pdf_slot4)
        show_late_pdf = bool(self._folder_action_show_late_pdf)

        if orders_form_zone_2 is not None:
            orders_form_zone_2.setVisible(show_orders_form_slot2)
        if orders_form_zone_3 is not None:
            orders_form_zone_3.setVisible(show_orders_form_slot3)
        if orders_form_zone_4 is not None:
            orders_form_zone_4.setVisible(show_orders_form_slot4)
        if orders_form_add_button is not None:
            orders_form_add_button.setVisible(not show_orders_form_slot4)

        if pdf_zone_2 is not None:
            pdf_zone_2.setVisible(show_pdf_slot2)
        if pdf_zone_3 is not None:
            pdf_zone_3.setVisible(show_pdf_slot3)
        if pdf_zone_4 is not None:
            pdf_zone_4.setVisible(show_pdf_slot4)
        if late_pdf_zone is not None:
            late_pdf_zone.setVisible(show_late_pdf)
        if pdf_add_button is not None:
            pdf_add_button.setVisible(not show_pdf_slot4)

    def _on_folder_action_expand_second_card(self, asset_kind: str) -> None:
        kind = str(asset_kind or "").strip().lower()
        expanded = False
        if kind == "orders_form":
            if not self._folder_action_show_orders_form_slot2:
                self._folder_action_show_orders_form_slot2 = True
                expanded = True
            elif not self._folder_action_show_orders_form_slot3:
                self._folder_action_show_orders_form_slot3 = True
                expanded = True
            elif not self._folder_action_show_orders_form_slot4:
                self._folder_action_show_orders_form_slot4 = True
                expanded = True
        elif kind == "pdf":
            if not self._folder_action_show_pdf_slot2:
                self._folder_action_show_pdf_slot2 = True
                expanded = True
            elif not self._folder_action_show_pdf_slot3:
                self._folder_action_show_pdf_slot3 = True
                expanded = True
            elif not self._folder_action_show_pdf_slot4:
                self._folder_action_show_pdf_slot4 = True
                expanded = True
            else:
                return
        else:
            return
        if kind == "pdf" and expanded:
            self._folder_action_pdf_click_guard_until = time.monotonic() + 0.35
        self._refresh_optional_asset_cards_visibility()
        anchor_id = self.folder_action_anchor_item_id
        if anchor_id is not None:
            self._refresh_folder_action_asset_states(int(anchor_id))

    def _resolve_action_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path(self.base_dir) / path).resolve()
        return path

    def _build_unique_file_path(self, parent_dir: Path, file_name: str) -> Path:
        source = Path(file_name)
        suffix = "".join(source.suffixes)
        stem = source.name[: -len(suffix)] if suffix else source.name
        next_stem = self._next_available_generated_stem(
            parent_dir,
            stem,
            (suffix if suffix else "",),
            counter_prefix=" ",
        )
        return (parent_dir / f"{next_stem}{suffix}").resolve()

    def _next_available_generated_stem(
        self,
        parent_dir: Path,
        base_stem: str,
        suffixes: tuple[str, ...],
        *,
        counter_prefix: str = "",
    ) -> str:
        stem = str(base_stem or "").strip() or "output"
        normalized_suffixes = tuple(
            s if str(s).startswith(".") else f".{s}"
            for s in suffixes
            if str(s).strip()
        ) or ("",)

        def _has_conflict(candidate_stem: str) -> bool:
            for suffix in normalized_suffixes:
                if (parent_dir / f"{candidate_stem}{suffix}").exists():
                    return True
            return False

        if not _has_conflict(stem):
            return stem

        i = 1
        while True:
            candidate = f"{stem}{counter_prefix}({i})"
            if not _has_conflict(candidate):
                return candidate
            i += 1

    def _ask_generate_conflict_action(
        self,
        *,
        title: str,
        existing_paths: list[Path],
    ) -> str:
        details = "\n".join(str(p) for p in existing_paths)
        answer = self._ask_question_topmost_non_modal(
            "File Already Exists",
            (
                f"A generated file for {title} already exists in this folder.\n\n"
                "What would you like to do?\n"
                "- Replace: overwrite the existing file\n"
                "- Rename: keep both files using (1), (2), (3)\n"
                "- Cancel: stop this generate action\n\n"
                f"Existing target(s):\n{details}"
            ),
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
            button_labels={
                int(QMessageBox.Yes): "Replace",
                int(QMessageBox.No): "Rename",
                int(QMessageBox.Cancel): "Cancel",
            },
            close_result=int(QMessageBox.Cancel),
        )
        if answer == int(QMessageBox.Yes):
            return "replace"
        if answer == int(QMessageBox.No):
            return "rename"
        return "cancel"

    def _prepare_asset_path_for_link(self, folder_dir: Path, selected_path: Path, *, asset_label: str) -> Path | None:
        picked_path = selected_path.resolve()
        if self._path_is_within_folder(picked_path, folder_dir):
            return picked_path

        answer = self._ask_question_topmost_non_modal(
            "Copy File Into Order Folder",
            (
                f"This {asset_label} file is outside the order folder.\n\n"
                f"Selected file:\n{picked_path}\n\n"
                f"Order folder:\n{folder_dir}\n\n"
                "Copy into this folder and link it?"
            ),
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
            button_labels={
                int(QMessageBox.Yes): "Copy && Link",
                int(QMessageBox.No): "Link Original",
                int(QMessageBox.Cancel): "Cancel",
            },
            close_result=int(QMessageBox.Cancel),
        )
        if answer == QMessageBox.Cancel:
            return None
        if answer == QMessageBox.No:
            return picked_path

        target_path = self._build_unique_file_path(folder_dir, picked_path.name)
        self._run_blocking_io_task(
            "Copy File",
            f"Copying {asset_label} into order folder...\nPlease wait.",
            lambda: self._run_fs_with_retry(
                f"copy {asset_label} into order folder {picked_path} -> {target_path}",
                lambda: shutil.copy2(str(picked_path), str(target_path)),
            ),
        )
        return target_path.resolve()

    def _asset_duplicate_scope_name(self, asset_kind: str) -> str:
        kind = str(asset_kind or "").strip().lower()
        if kind in {"pdf", "pdf_2", "pdf_3", "pdf_4", "late_pdf"}:
            return "PDF"
        if kind in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4"}:
            return "Make Orders Form"
        return self._asset_kind_label(kind)

    def _asset_duplicate_scope_slots(self, asset_kind: str) -> str:
        kind = str(asset_kind or "").strip().lower()
        if kind in {"pdf", "pdf_2", "pdf_3", "pdf_4", "late_pdf"}:
            return "PDF1/PDF2/PDF3/PDF4/Late PDF"
        if kind in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4"}:
            return "Make Orders Form #1/#2/#3/#4"
        return self._asset_kind_label(kind)

    def _asset_path_key(self, path: Path) -> str:
        return str(path).replace("\\", "/").lower()

    def _pick_asset_file(
        self,
        *,
        folder_dir: Path,
        asset_label: str,
        dialog_title: str,
        file_filter: str,
        asset_kind: str,
        disallowed_paths: list[Path] | None = None,
    ) -> Path | None:
        blocked_keys: set[str] = set()
        for candidate in disallowed_paths or []:
            try:
                resolved = candidate.expanduser().resolve()
            except Exception:
                resolved = candidate
            blocked_keys.add(self._asset_path_key(resolved))

        while True:
            picked = ""
            if blocked_keys:
                dialog = QFileDialog(self, dialog_title, str(folder_dir), file_filter)
                dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
                dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
                dialog.setNameFilter(file_filter)
                proxy = _AssetPathBlacklistProxyModel(blocked_keys, dialog)
                try:
                    dialog.setProxyModel(proxy)
                except Exception:
                    pass
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    selected_files = dialog.selectedFiles()
                    if selected_files:
                        picked = str(selected_files[0] or "")
            else:
                picked, _ = QFileDialog.getOpenFileName(
                    self,
                    dialog_title,
                    str(folder_dir),
                    file_filter,
                )
            if not picked:
                return None
            if Path(picked).suffix.lower() not in self._asset_allowed_suffixes(asset_kind):
                allowed_text = self._asset_allowed_text(asset_kind)
                self._show_user_error_dialog(
                    f"Invalid {asset_label} File",
                    f"The selected file does not match the {asset_label} card.",
                    possible_causes=[f"This card only accepts {allowed_text} files."],
                    next_steps=[f"Choose a valid {asset_label} file and try again."],
                    details=str(picked),
                )
                continue
            prepared = self._prepare_asset_path_for_link(folder_dir, Path(picked), asset_label=asset_label)
            if prepared is None:
                return None

            blocked_key = self._asset_path_key(prepared)
            if blocked_key in blocked_keys:
                scope_name = self._asset_duplicate_scope_name(asset_kind)
                scope_slots = self._asset_duplicate_scope_slots(asset_kind)
                self._show_user_error_dialog(
                    f"{asset_label} Already Linked",
                    f"This {scope_name} file is already linked in {scope_slots} for this folder.",
                    next_steps=[f"Choose a different {scope_name} file."],
                    details=str(prepared),
                )
                continue
            return prepared

    def _save_asset_path_for_item(self, item_id: int, asset_kind: str, selected_path: Path) -> None:
        if asset_kind == "pdf":
            self.db.set_pdf_path(item_id, str(selected_path))
            return
        if asset_kind == "pdf_2":
            self.db.set_pdf_path_2(item_id, str(selected_path))
            return
        if asset_kind == "pdf_3":
            self.db.set_pdf_path_3(item_id, str(selected_path))
            return
        if asset_kind == "pdf_4":
            self.db.set_pdf_path_4(item_id, str(selected_path))
            return
        if asset_kind == "late_pdf":
            self.db.set_late_pdf_path(item_id, str(selected_path))
            return
        if asset_kind == "excel":
            self.db.set_excel_path(item_id, str(selected_path))
            return
        if asset_kind == "orders_form":
            self.db.set_orders_form_path(item_id, str(selected_path))
            return
        if asset_kind == "orders_form_2":
            self.db.set_orders_form_path_2(item_id, str(selected_path))
            return
        if asset_kind == "orders_form_3":
            self.db.set_orders_form_path_3(item_id, str(selected_path))
            return
        if asset_kind == "orders_form_4":
            self.db.set_orders_form_path_4(item_id, str(selected_path))
            return
        if asset_kind == "qr_roster":
            self.db.set_qr_roster_path(item_id, str(selected_path))
            return
        if asset_kind == "qr_orders":
            self.db.set_qr_orders_path(item_id, str(selected_path))
            return
        raise ValueError(f"Unsupported asset kind: {asset_kind}")

    def _raw_asset_path_from_item(self, db_item, asset_kind: str) -> str:
        kind = str(asset_kind or "").strip().lower()
        if kind == "pdf":
            return str(db_item.pdf_path or "").strip()
        if kind == "pdf_2":
            return str(getattr(db_item, "pdf_path_2", "") or "").strip()
        if kind == "pdf_3":
            return str(getattr(db_item, "pdf_path_3", "") or "").strip()
        if kind == "pdf_4":
            return str(getattr(db_item, "pdf_path_4", "") or "").strip()
        if kind == "late_pdf":
            return str(getattr(db_item, "late_pdf_path", "") or "").strip()
        if kind == "excel":
            return str(db_item.excel_path or "").strip()
        if kind == "orders_form":
            return str(db_item.orders_form_path or "").strip()
        if kind == "orders_form_2":
            return str(getattr(db_item, "orders_form_path_2", "") or "").strip()
        if kind == "orders_form_3":
            return str(getattr(db_item, "orders_form_path_3", "") or "").strip()
        if kind == "orders_form_4":
            return str(getattr(db_item, "orders_form_path_4", "") or "").strip()
        if kind == "qr_roster":
            return str(db_item.qr_roster_path or "").strip()
        if kind == "qr_orders":
            return str(db_item.qr_orders_path or "").strip()
        return ""

    def _asset_presence_flags_from_item(
        self,
        db_item,
        *,
        override: dict[str, bool | None] | None = None,
    ) -> dict[str, bool]:
        has_orders_form = (
            self._asset_link_exists(self._raw_asset_path_from_item(db_item, "orders_form"))
            or self._asset_link_exists(self._raw_asset_path_from_item(db_item, "orders_form_2"))
            or self._asset_link_exists(self._raw_asset_path_from_item(db_item, "orders_form_3"))
            or self._asset_link_exists(self._raw_asset_path_from_item(db_item, "orders_form_4"))
        )
        has_pdf = (
            self._asset_link_exists(self._raw_asset_path_from_item(db_item, "pdf"))
            or self._asset_link_exists(self._raw_asset_path_from_item(db_item, "pdf_2"))
            or self._asset_link_exists(self._raw_asset_path_from_item(db_item, "pdf_3"))
            or self._asset_link_exists(self._raw_asset_path_from_item(db_item, "pdf_4"))
        )
        has_late_pdf = self._asset_link_exists(self._raw_asset_path_from_item(db_item, "late_pdf"))
        values = {
            "has_orders_form": has_orders_form,
            "has_pdf": has_pdf,
            "has_late_pdf": has_late_pdf,
            "has_excel": self._asset_link_exists(self._raw_asset_path_from_item(db_item, "excel")),
            "has_qr_roster": self._asset_link_exists(self._raw_asset_path_from_item(db_item, "qr_roster")),
            "has_qr_orders": self._asset_link_exists(self._raw_asset_path_from_item(db_item, "qr_orders")),
        }
        for key, value in (override or {}).items():
            if key in values:
                if value is None:
                    continue
                values[key] = bool(value)
        return values

    def _pick_orders_form_example_psd(self, folder_dir: Path) -> Path | None:
        settings = self._column_size_settings()
        settings_key = "assets/orders_form_example_psd"
        saved_value = str(settings.value(settings_key, "") or "").strip()
        start_dir = folder_dir
        if saved_value:
            saved_path = Path(saved_value).expanduser()
            if saved_path.exists():
                start_dir = saved_path.parent if saved_path.is_file() else saved_path
        else:
            default_psd_dir = Path(DEFAULT_PSD_DIR).expanduser()
            if default_psd_dir.exists():
                start_dir = default_psd_dir

        picked, _ = QFileDialog.getOpenFileName(
            self,
            "Select Make Orders Form Example PSD",
            str(start_dir),
            "Photoshop Files (*.psd);;All Files (*.*)",
        )
        if not picked:
            return None
        selected = Path(picked).expanduser().resolve()
        if selected.suffix.lower() != ".psd":
            self._show_user_error_dialog(
                "Invalid Example PSD",
                "The selected example file must be a .psd template.",
                next_steps=["Choose a valid .psd file and try again."],
                details=str(selected),
            )
            return None
        settings.setValue(settings_key, str(selected))
        return selected

    def _extract_orders_form_generation_payload(self, db_item) -> tuple[str, str, str]:
        display = str(getattr(db_item, "display_name", "") or "").strip()
        if not display:
            display = normalize_display_name(str(getattr(db_item, "disk_name", "") or "")).strip()

        match = re.match(r"^\s*(\d{6})\b", display)
        if not match:
            raise ValueError(f"Could not parse yymmdd from folder name: {display}")
        yymmdd = match.group(1)
        mmddyy = f"{yymmdd[2:4]}{yymmdd[4:6]}{yymmdd[0:2]}"

        pid = str(getattr(db_item, "pid", "") or "").strip().upper()
        if not pid:
            pid = str(parse_job_code(display) or "").strip().upper()
        if not pid:
            raise ValueError(f"Could not parse PID from folder name: {display}")

        school_name = re.sub(r"^\s*\d{6}\s+", "", display)
        school_name = re.sub(r"\s+P\d{8,}\b.*$", "", school_name, flags=re.IGNORECASE).strip()
        if not school_name:
            raise ValueError(f"Could not parse school name from folder name: {display}")
        return pid, mmddyy, school_name

    def _generate_orders_form_from_example(
        self,
        *,
        item_id: int,
        db_item,
        folder_dir: Path,
        asset_kind: str = "orders_form",
    ) -> Path | None:
        kind = str(asset_kind or "orders_form").strip().lower()
        if kind not in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4"}:
            return None
        slot_labels = {
            "orders_form": "Make Orders Form",
            "orders_form_2": "Make Orders Form #2",
            "orders_form_3": "Make Orders Form #3",
            "orders_form_4": "Make Orders Form #4",
        }
        slot_suffixes = {
            "orders_form": "",
            "orders_form_2": " 2",
            "orders_form_3": " 3",
            "orders_form_4": " 4",
        }
        slot_label = slot_labels[kind]
        slot_suffix = slot_suffixes[kind]
        base_stem = f"{folder_dir.name}{slot_suffix}".replace(" ", "-")
        output_folder_name = base_stem
        generated = (folder_dir / f"{output_folder_name}.jpg").resolve()
        replacing_existing = False
        replace_backup_path: Path | None = None
        if generated.exists():
            action = self._ask_generate_conflict_action(
                title=slot_label,
                existing_paths=[generated],
            )
            if action == "cancel":
                self._set_completion_status(f"{slot_label} cancelled.")
                return None
            if action == "rename":
                output_folder_name = self._next_available_generated_stem(folder_dir, base_stem, (".jpg",))
                generated = (folder_dir / f"{output_folder_name}.jpg").resolve()
            else:
                replacing_existing = True

        example_psd = self._pick_orders_form_example_psd(folder_dir)
        if example_psd is None:
            return None
        try:
            pid, mmddyy, school_name = self._extract_orders_form_generation_payload(db_item)
        except Exception as exc:
            self._show_user_error_dialog(
                "Make Orders Form Generation Failed",
                "DAMYComp could not parse required values from the selected folder name.",
                possible_causes=[
                    "The folder name is missing expected date/PID tokens.",
                    "The workflow row was renamed to a non-standard format.",
                ],
                next_steps=["Use manual link for this folder, or rename it to the standard format."],
                details=str(exc),
            )
            return None

        if replacing_existing and generated.exists():
            replace_backup_path = self._build_unique_file_path(folder_dir, f"{generated.name}.bak")
            try:
                shutil.move(str(generated), str(replace_backup_path))
            except Exception as exc:
                self._show_user_error_dialog(
                    f"Generate {slot_label} Failed",
                    f"DAMYComp could not prepare the existing {slot_label} JPG for replacement.",
                    possible_causes=[
                        "The existing JPG is open in another program.",
                        "Folder write permissions are temporarily unavailable.",
                    ],
                    next_steps=["Close any app using this JPG and try again."],
                    details=str(exc),
                )
                return None

        try:
            calendar_module = importlib.import_module("folder_manager.calendar_import_v3.main")
            form_creator_fn = getattr(calendar_module, "form_creator", None)
            if not callable(form_creator_fn):
                raise RuntimeError("calendar_import_v3.form_creator is unavailable.")
            self._run_blocking_io_task(
                f"Generate {slot_label}",
                f"Generating {slot_label} JPG from example PSD...\nPlease wait.",
                lambda: form_creator_fn(
                    str(folder_dir),
                    output_folder_name,
                    pid,
                    mmddyy,
                    school_name,
                    str(example_psd),
                ),
            )
        except Exception as exc:
            if replace_backup_path is not None and replace_backup_path.exists():
                try:
                    if generated.exists():
                        generated.unlink()
                    shutil.move(str(replace_backup_path), str(generated))
                except Exception:
                    pass
            self._show_user_error_dialog(
                f"Generate {slot_label} Failed",
                f"DAMYComp could not generate {slot_label} from the selected example PSD.",
                possible_causes=[
                    "Photoshop COM integration is unavailable on this machine.",
                    "The PSD template does not contain expected text layers.",
                    "The selected PSD file is locked or invalid.",
                ],
                next_steps=[
                    "Close Photoshop and retry.",
                    "Choose another known-good example PSD template.",
                    "If needed, link an existing JPG manually.",
                ],
                details=str(exc),
            )
            return None

        if not generated.exists():
            if replace_backup_path is not None and replace_backup_path.exists():
                try:
                    shutil.move(str(replace_backup_path), str(generated))
                except Exception:
                    pass
            self._show_user_error_dialog(
                f"Generate {slot_label} Failed",
                f"DAMYComp finished generate but did not find the expected {slot_label} JPG.",
                next_steps=["Retry generate, or choose a file manually by double-clicking the card."],
                details=str(generated),
            )
            return None
        if replace_backup_path is not None and replace_backup_path.exists():
            try:
                replace_backup_path.unlink()
            except Exception:
                pass

        try:
            if kind == "orders_form_2":
                self.db.set_orders_form_path_2(item_id, str(generated))
            elif kind == "orders_form_3":
                self.db.set_orders_form_path_3(item_id, str(generated))
            elif kind == "orders_form_4":
                self.db.set_orders_form_path_4(item_id, str(generated))
            else:
                self.db.set_orders_form_path(item_id, str(generated))
        except Exception as exc:
            self._show_user_error_dialog(
                f"Save {slot_label} Path Failed",
                f"DAMYComp generated {slot_label} but could not save the path in DB.",
                possible_causes=[
                    "The database connection was interrupted.",
                    "The workflow row changed while this action was running.",
                ],
                next_steps=["Refresh the board and relink the generated JPG manually if needed."],
                details=str(exc),
            )
            return None
        self._update_asset_marker_for_item(
            item_id,
            **self._asset_presence_flags_from_item(
                db_item,
                override={"has_orders_form": generated.exists()},
            ),
        )
        self._refresh_folder_action_asset_states(item_id)
        self._set_completion_status(f"{slot_label} generated and linked.")
        return generated

    def _ensure_asset_path_for_qr(
        self,
        *,
        item_id: int,
        db_item,
        folder_dir: Path,
        asset_kind: str,
        title: str,
    ) -> Path | None:
        asset_kind = str(asset_kind or "").strip().lower()
        if asset_kind not in {"pdf", "excel"}:
            return None

        raw_value = (db_item.pdf_path or "").strip() if asset_kind == "pdf" else (db_item.excel_path or "").strip()
        if raw_value:
            resolved = self._resolve_action_path(raw_value)
            if resolved.exists():
                return resolved

        selected_path = self._pick_asset_file(
            folder_dir=folder_dir,
            asset_label=asset_kind.upper(),
            dialog_title=f"{title}: Select {asset_kind.upper()} File",
            file_filter=self._asset_filter_text(asset_kind),
            asset_kind=asset_kind,
        )
        if selected_path is None:
            return None

        try:
            self._save_asset_path_for_item(item_id, asset_kind, selected_path)
        except Exception as exc:
            self._show_user_error_dialog(
                f"Save {asset_kind.upper()} Path Failed",
                f"DAMYComp could not save the {asset_kind.upper()} path for this QR action.",
                possible_causes=[
                    "The database connection was interrupted.",
                    "The workflow row changed while the file picker was open.",
                ],
                next_steps=["Refresh the board and try again."],
                details=str(exc),
            )
            return None

        self._update_asset_marker_for_item(
            item_id,
            **self._asset_presence_flags_from_item(
                db_item,
                override={
                    "has_pdf": selected_path.exists() if asset_kind == "pdf" else None,
                    "has_excel": selected_path.exists() if asset_kind == "excel" else None,
                },
            ),
        )
        self._refresh_folder_action_asset_states(item_id)
        return selected_path

    def _collect_linked_pdf_paths_for_qr(
        self,
        db_item,
        *,
        preferred_first: Path | None = None,
    ) -> list[Path]:
        candidates = []
        if preferred_first is not None:
            try:
                preferred_resolved = preferred_first.expanduser().resolve()
            except Exception:
                preferred_resolved = preferred_first
            if preferred_resolved.exists():
                candidates.append(preferred_resolved)

        raw_values = [
            str(getattr(db_item, "pdf_path", "") or "").strip(),
            str(getattr(db_item, "pdf_path_2", "") or "").strip(),
            str(getattr(db_item, "pdf_path_3", "") or "").strip(),
            str(getattr(db_item, "pdf_path_4", "") or "").strip(),
            str(getattr(db_item, "late_pdf_path", "") or "").strip(),
        ]
        for raw in raw_values:
            if not raw:
                continue
            resolved = self._resolve_action_path(raw)
            if not resolved.exists():
                continue
            candidates.append(resolved)

        deduped = []
        seen = set()
        for path in candidates:
            key = self._asset_path_key(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _collect_linked_orders_form_paths(
        self,
        db_item,
        *,
        preferred_first: Path | None = None,
    ) -> list[Path]:
        candidates = []
        if preferred_first is not None:
            try:
                preferred_resolved = preferred_first.expanduser().resolve()
            except Exception:
                preferred_resolved = preferred_first
            if preferred_resolved.exists():
                candidates.append(preferred_resolved)

        raw_values = [
            str(getattr(db_item, "orders_form_path", "") or "").strip(),
            str(getattr(db_item, "orders_form_path_2", "") or "").strip(),
            str(getattr(db_item, "orders_form_path_3", "") or "").strip(),
            str(getattr(db_item, "orders_form_path_4", "") or "").strip(),
        ]
        for raw in raw_values:
            if not raw:
                continue
            resolved = self._resolve_action_path(raw)
            if not resolved.exists():
                continue
            candidates.append(resolved)

        deduped = []
        seen = set()
        for path in candidates:
            key = self._asset_path_key(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _collect_same_type_linked_paths(self, db_item, asset_kind: str) -> list[Path]:
        kind = str(asset_kind or "").strip().lower()
        if kind in {"pdf", "pdf_2", "pdf_3", "pdf_4"}:
            return self._collect_linked_pdf_paths_for_qr(db_item)
        if kind == "late_pdf":
            raw = str(getattr(db_item, "late_pdf_path", "") or "").strip()
            if not raw:
                return []
            resolved = self._resolve_action_path(raw)
            return [resolved] if resolved.exists() else []
        if kind in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4"}:
            return self._collect_linked_orders_form_paths(db_item)
        return []

    def _get_current_folder_action_target(self, *, action_name: str) -> tuple[int, object, Path] | None:
        item_id = self.folder_action_anchor_item_id
        if item_id is None:
            return None

        db_item = None
        cached_item = self.folder_action_anchor_db_item
        try:
            if cached_item is not None and int(getattr(cached_item, "id", -1)) == int(item_id):
                db_item = cached_item
        except Exception:
            db_item = None

        if db_item is None:
            try:
                db_item = self.db.get_item_by_id(int(item_id))
            except Exception as exc:
                self._show_user_error_dialog(
                    f"{action_name} Failed",
                    "DAMYComp could not read this workflow row from the database.",
                    possible_causes=[
                        "The DB connection was interrupted.",
                        "The row was changed by another process.",
                    ],
                    next_steps=["Refresh the board and try again."],
                    details=str(exc),
                )
                return None
        if not db_item:
            self._show_user_error_dialog(
                f"{action_name} Failed",
                "This workflow row no longer exists in the database.",
                next_steps=["Refresh the board and try again."],
            )
            return None
        self.folder_action_anchor_db_item = db_item

        folder_dir = self._resolve_existing_folder_path(db_item.disk_name)
        if folder_dir is None:
            self._show_user_error_dialog(
                f"{action_name} Failed",
                "DAMYComp could not find this order folder in the expected DAMY locations.",
                possible_causes=[
                    "The folder was moved, renamed, or deleted.",
                    "The DAMY network location is not fully available.",
                ],
                next_steps=["Check the folder on disk, then refresh the board."],
                details=(
                    "Checked locations:\n"
                    f"- {self.base_dir}\\{db_item.disk_name}\n"
                    f"- {self.base_dir}\\cancel\\{db_item.disk_name}\n"
                    f"- {self.source_base_dir or '(source not set)'}\\{db_item.disk_name}\n"
                    f"- {self.source_base_dir or '(source not set)'}\\cancel\\{db_item.disk_name}"
                ),
            )
            return None
        return int(item_id), db_item, folder_dir

    def _link_asset_for_current_item(
        self,
        asset_kind: str,
        selected_source: Path | None = None,
        *,
        open_existing_if_available: bool = False,
    ) -> None:
        asset_kind = str(asset_kind or "").strip().lower()
        if asset_kind not in {
            "orders_form",
            "orders_form_2",
            "orders_form_3",
            "orders_form_4",
            "pdf",
            "pdf_2",
            "pdf_3",
            "pdf_4",
            "late_pdf",
            "excel",
            "qr_roster",
            "qr_orders",
        }:
            return

        label = self._asset_kind_label(asset_kind)
        action_name = f"Open {label}"
        target = self._get_current_folder_action_target(action_name=action_name)
        if target is None:
            return
        item_id, db_item, folder_dir = target

        existing_raw = self._raw_asset_path_from_item(db_item, asset_kind)
        if selected_source is None and open_existing_if_available:
            if existing_raw:
                existing_path = self._resolve_action_path(existing_raw)
                if existing_path.exists():
                    try:
                        self._open_path_in_system(existing_path)
                        return
                    except Exception as exc:
                        self._show_user_error_dialog(
                            f"{action_name} Failed",
                            f"DAMYComp found the linked {label} file but could not open it.",
                            possible_causes=[
                                "The file is locked by another program.",
                                "Windows does not have a working default app for this file type.",
                            ],
                            next_steps=[
                                "Close any app using the file and try again.",
                                "If needed, choose the file again.",
                            ],
                            details=str(existing_path),
                        )
                        return
                self._show_user_error_dialog(
                    f"{label} File Missing",
                    f"The saved {label} path no longer exists.",
                    possible_causes=[
                        "The file was moved, renamed, or deleted.",
                        "The DAMY folder is not fully available right now.",
                    ],
                    next_steps=["Choose the file again to relink it."],
                    details=existing_raw,
                )

        disallowed_paths = self._collect_same_type_linked_paths(db_item, asset_kind)
        if selected_source is None:
            selected_path = self._pick_asset_file(
                folder_dir=folder_dir,
                asset_label=label,
                dialog_title=f"Select {label} File",
                file_filter=self._asset_filter_text(asset_kind),
                asset_kind=asset_kind,
                disallowed_paths=disallowed_paths,
            )
        else:
            selected_path = self._prepare_asset_path_for_link(folder_dir, selected_source, asset_label=label)

        if selected_path is None:
            return
        if disallowed_paths:
            blocked_keys = {self._asset_path_key(candidate) for candidate in disallowed_paths}
            if self._asset_path_key(selected_path) in blocked_keys:
                scope_name = self._asset_duplicate_scope_name(asset_kind)
                scope_slots = self._asset_duplicate_scope_slots(asset_kind)
                self._show_user_error_dialog(
                    f"{label} Already Linked",
                    f"This {scope_name} file is already linked in {scope_slots} for this folder.",
                    next_steps=[f"Choose a different {scope_name} file."],
                    details=str(selected_path),
                )
                return

        try:
            self._save_asset_path_for_item(item_id, asset_kind, selected_path)
        except Exception as exc:
            self._show_user_error_dialog(
                f"Save {label} Path Failed",
                f"DAMYComp could not save the {label} path to the workflow database.",
                possible_causes=[
                    "The database connection was interrupted.",
                    "The row was changed by another process.",
                ],
                next_steps=["Refresh the board and try again."],
                details=str(exc),
            )
            return

        try:
            db_item_after = self.db.get_item_by_id(int(item_id))
        except Exception:
            db_item_after = db_item
        if db_item_after:
            self._update_asset_marker_for_item(
                item_id,
                **self._asset_presence_flags_from_item(db_item_after),
            )
        self._refresh_folder_action_asset_states(item_id)

    def _refresh_folder_action_asset_states(self, item_id: int) -> None:
        try:
            db_item = self.db.get_item_by_id(int(item_id))
        except Exception:
            db_item = None
        if not db_item:
            return
        try:
            if self.folder_action_anchor_item_id is not None and int(self.folder_action_anchor_item_id) == int(item_id):
                self.folder_action_anchor_db_item = db_item
        except Exception:
            pass
        orders_form_zone = getattr(self, "folder_action_make_orders_zone", None)
        orders_form_zone_2 = getattr(self, "folder_action_make_orders_zone_2", None)
        orders_form_zone_3 = getattr(self, "folder_action_make_orders_zone_3", None)
        orders_form_zone_4 = getattr(self, "folder_action_make_orders_zone_4", None)
        pdf_zone = getattr(self, "folder_action_pdf_zone", None)
        pdf_zone_2 = getattr(self, "folder_action_pdf_zone_2", None)
        pdf_zone_3 = getattr(self, "folder_action_pdf_zone_3", None)
        pdf_zone_4 = getattr(self, "folder_action_pdf_zone_4", None)
        late_pdf_zone = getattr(self, "folder_action_late_pdf_zone", None)
        excel_zone = getattr(self, "folder_action_excel_zone", None)
        qr_roster_zone = getattr(self, "folder_action_qr_roster_zone", None)
        qr_orders_zone = getattr(self, "folder_action_qr_orders_zone", None)

        if str(self._raw_asset_path_from_item(db_item, "orders_form_2")).strip():
            self._folder_action_show_orders_form_slot2 = True
        if str(self._raw_asset_path_from_item(db_item, "orders_form_3")).strip():
            self._folder_action_show_orders_form_slot2 = True
            self._folder_action_show_orders_form_slot3 = True
        if str(self._raw_asset_path_from_item(db_item, "orders_form_4")).strip():
            self._folder_action_show_orders_form_slot2 = True
            self._folder_action_show_orders_form_slot3 = True
            self._folder_action_show_orders_form_slot4 = True
        if str(self._raw_asset_path_from_item(db_item, "pdf_2")).strip():
            self._folder_action_show_pdf_slot2 = True
        if str(self._raw_asset_path_from_item(db_item, "pdf_3")).strip():
            self._folder_action_show_pdf_slot2 = True
            self._folder_action_show_pdf_slot3 = True
        if str(self._raw_asset_path_from_item(db_item, "pdf_4")).strip():
            self._folder_action_show_pdf_slot2 = True
            self._folder_action_show_pdf_slot3 = True
            self._folder_action_show_pdf_slot4 = True
        if str(self._raw_asset_path_from_item(db_item, "late_pdf")).strip():
            self._folder_action_show_late_pdf = True
        self._refresh_optional_asset_cards_visibility()

        def _unlinked_hint_for(asset_kind: str) -> str:
            kind = str(asset_kind or "").strip().lower()
            if kind in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4", "qr_roster", "qr_orders"}:
                return (
                    "Drop a file here\n"
                    "or single-click to generate\n"
                    "Double-click to choose file"
                )
            return (
                "Drop a file here\n"
                "or single-click to choose file\n"
                "Double-click to choose file"
            )

        def _missing_hint_for(asset_kind: str) -> str:
            kind = str(asset_kind or "").strip().lower()
            if kind in {"orders_form", "orders_form_2", "orders_form_3", "orders_form_4", "qr_roster", "qr_orders"}:
                return "Saved path is missing\nSingle-click to generate"
            return "Saved path is missing\nSingle-click to choose again"

        def _apply_zone_state(zone: AssetDropZone | None, asset_kind: str) -> None:
            if zone is None:
                return
            _path, file_name, meta_text, exists = self._describe_asset_file(
                self._raw_asset_path_from_item(db_item, asset_kind)
            )
            zone.set_asset_state(
                linked=bool(exists),
                file_name=file_name,
                meta_text=meta_text,
                status_text="Linked" if exists else ("Path missing" if file_name else "Not linked"),
                hint_text=(
                    f"{file_name}\nSingle-click to open"
                    if exists and file_name
                    else (
                        _missing_hint_for(asset_kind)
                        if file_name
                        else _unlinked_hint_for(asset_kind)
                    )
                ),
            )

        _apply_zone_state(orders_form_zone, "orders_form")
        _apply_zone_state(orders_form_zone_2, "orders_form_2")
        _apply_zone_state(orders_form_zone_3, "orders_form_3")
        _apply_zone_state(orders_form_zone_4, "orders_form_4")
        _apply_zone_state(pdf_zone, "pdf")
        _apply_zone_state(pdf_zone_2, "pdf_2")
        _apply_zone_state(pdf_zone_3, "pdf_3")
        _apply_zone_state(pdf_zone_4, "pdf_4")
        _apply_zone_state(late_pdf_zone, "late_pdf")
        _apply_zone_state(excel_zone, "excel")
        _apply_zone_state(qr_roster_zone, "qr_roster")
        _apply_zone_state(qr_orders_zone, "qr_orders")

    def _on_folder_action_asset_dropped(self, asset_kind: str, raw_path: str) -> None:
        asset_kind = str(asset_kind or "").strip().lower()
        if asset_kind not in {
            "orders_form",
            "orders_form_2",
            "orders_form_3",
            "orders_form_4",
            "pdf",
            "pdf_2",
            "pdf_3",
            "pdf_4",
            "late_pdf",
            "excel",
            "qr_roster",
            "qr_orders",
        }:
            return
        file_path = Path(str(raw_path or "")).expanduser()
        if not file_path.exists():
            self._show_user_error_dialog(
                "Dropped File Missing",
                "DAMYComp could not find the file you dropped.",
                possible_causes=["The file was moved or removed before the drop finished."],
                next_steps=["Try dragging the file again."],
                details=str(file_path),
            )
            return

        suffix = file_path.suffix.lower()
        allowed_suffixes = self._asset_allowed_suffixes(asset_kind)
        if not allowed_suffixes or suffix not in allowed_suffixes:
            label = self._asset_kind_label(asset_kind)
            allowed_text = self._asset_allowed_text(asset_kind)
            self._show_user_error_dialog(
                f"Invalid {label} File",
                f"The dropped file does not match the {label} card.",
                possible_causes=[f"This card only accepts {allowed_text} files."],
                next_steps=[f"Drop a valid {label} file onto this card."],
                details=str(file_path),
            )
            return

        self._link_asset_for_current_item(asset_kind, file_path)

    def _open_linked_asset_for_current_item(self, asset_kind: str) -> None:
        self._link_asset_for_current_item(asset_kind, open_existing_if_available=True)

    def _clear_asset_for_current_item(self, asset_kind: str) -> None:
        asset_kind = str(asset_kind or "").strip().lower()
        if asset_kind not in {
            "orders_form",
            "orders_form_2",
            "orders_form_3",
            "orders_form_4",
            "pdf",
            "pdf_2",
            "pdf_3",
            "pdf_4",
            "late_pdf",
            "excel",
            "qr_roster",
            "qr_orders",
        }:
            return
        label = self._asset_kind_label(asset_kind)
        target = self._get_current_folder_action_target(action_name=f"Clear {label}")
        if target is None:
            return
        item_id, db_item, _folder_dir = target
        raw_value = self._raw_asset_path_from_item(db_item, asset_kind)
        if not raw_value:
            return
        if not self._confirm_dangerous_action(
            title=f"Clear {label} Link",
            happened=f"Remove the saved {label} link for this folder?",
            impacts=[
                "The file on disk will stay where it is.",
                f"The workflow DB will stop showing this folder as having a linked {label} file.",
            ],
            confirm_label="Clear Link",
            cancel_label="Keep Link",
        ):
            return
        try:
            if asset_kind == "pdf":
                self.db.set_pdf_path(item_id, None)
            elif asset_kind == "pdf_2":
                self.db.set_pdf_path_2(item_id, None)
            elif asset_kind == "pdf_3":
                self.db.set_pdf_path_3(item_id, None)
            elif asset_kind == "pdf_4":
                self.db.set_pdf_path_4(item_id, None)
            elif asset_kind == "late_pdf":
                self.db.set_late_pdf_path(item_id, None)
            elif asset_kind == "excel":
                self.db.set_excel_path(item_id, None)
            elif asset_kind == "orders_form":
                self.db.set_orders_form_path(item_id, None)
            elif asset_kind == "orders_form_2":
                self.db.set_orders_form_path_2(item_id, None)
            elif asset_kind == "orders_form_3":
                self.db.set_orders_form_path_3(item_id, None)
            elif asset_kind == "orders_form_4":
                self.db.set_orders_form_path_4(item_id, None)
            elif asset_kind == "qr_roster":
                self.db.set_qr_roster_path(item_id, None)
            elif asset_kind == "qr_orders":
                self.db.set_qr_orders_path(item_id, None)
        except Exception as exc:
            self._show_user_error_dialog(
                f"Clear {label} Link Failed",
                f"DAMYComp could not clear the saved {label} link.",
                possible_causes=[
                    "The database connection was interrupted.",
                    "The row was changed by another process.",
                ],
                next_steps=["Refresh the board and try again."],
                details=str(exc),
            )
            return
        try:
            db_item_after = self.db.get_item_by_id(int(item_id))
        except Exception:
            db_item_after = db_item
        if db_item_after:
            self._update_asset_marker_for_item(
                item_id,
                **self._asset_presence_flags_from_item(db_item_after),
            )
        if asset_kind == "orders_form_2":
            self._folder_action_show_orders_form_slot2 = False
            self._folder_action_show_orders_form_slot3 = False
            self._folder_action_show_orders_form_slot4 = False
        elif asset_kind == "orders_form_3":
            self._folder_action_show_orders_form_slot3 = False
            self._folder_action_show_orders_form_slot4 = False
        elif asset_kind == "orders_form_4":
            self._folder_action_show_orders_form_slot4 = False
        elif asset_kind == "pdf_2":
            self._folder_action_show_pdf_slot2 = False
            self._folder_action_show_pdf_slot3 = False
            self._folder_action_show_pdf_slot4 = False
        elif asset_kind == "pdf_3":
            self._folder_action_show_pdf_slot3 = False
            self._folder_action_show_pdf_slot4 = False
        elif asset_kind == "pdf_4":
            self._folder_action_show_pdf_slot4 = False
        elif asset_kind == "late_pdf":
            self._folder_action_show_late_pdf = False
        self._refresh_folder_action_asset_states(item_id)


    def _open_path_in_system(self, path: Path) -> None:
        if sys.platform.startswith("win"):
            os.startfile(str(path))
            return
        opener = "xdg-open" if sys.platform.startswith("linux") else "open"
        subprocess.Popen([opener, str(path)])

    def _find_item_by_id_in_list(self, lw: DragListWidget | None, item_id: int) -> QListWidgetItem | None:
        if lw is None:
            return None
        for idx in range(lw.count()):
            item = lw.item(idx)
            if item is None:
                continue
            raw_id = item.data(ROLE_DB_ID)
            try:
                if raw_id is not None and int(raw_id) == int(item_id):
                    return item
            except Exception:
                continue
        return None

    def _restore_folder_action_popup_for_item_id(self, lw: DragListWidget | None, item_id: int | None) -> None:
        if lw is None or item_id is None:
            return
        item = self._find_item_by_id_in_list(lw, int(item_id))
        if item is None:
            return
        self._show_folder_action_popup_for_item(lw, item)

    def _update_asset_marker_for_item(
        self,
        item_id: int,
        *,
        has_orders_form: bool,
        has_pdf: bool,
        has_late_pdf: bool,
        has_excel: bool,
        has_qr_roster: bool,
        has_qr_orders: bool,
    ) -> None:
        lw = self.folder_action_anchor_list
        item = self._find_item_by_id_in_list(lw, item_id)
        if lw is None or item is None:
            return
        lw.apply_asset_presence_style(
            item,
            has_orders_form=has_orders_form,
            has_pdf=has_pdf,
            has_late_pdf=has_late_pdf,
            has_excel=has_excel,
            has_qr_roster=has_qr_roster,
            has_qr_orders=has_qr_orders,
        )
        lw.viewport().update()

    def _resolve_existing_folder_path(self, disk_name: str) -> Path | None:
        disk = (disk_name or "").strip()
        if not disk:
            return None

        candidates: list[Path] = []
        base = Path(self.base_dir)
        candidates.append((base / disk).resolve())
        candidates.append((base / "cancel" / disk).resolve())

        source_dir = (self.source_base_dir or "").strip()
        if source_dir:
            source = Path(source_dir)
            candidates.append((source / disk).resolve())
            candidates.append((source / "cancel" / disk).resolve())

        for path in candidates:
            try:
                if path.is_dir():
                    return path
            except Exception:
                continue
        return None

    def _path_is_within_folder(self, file_path: Path, folder_dir: Path) -> bool:
        try:
            file_resolved = file_path.resolve()
            folder_resolved = folder_dir.resolve()
            file_resolved.relative_to(folder_resolved)
            return True
        except Exception:
            return False

    def _on_folder_action_excel_clicked(self) -> None:
        self._set_folder_action_inline_warning("")
        self._open_linked_asset_for_current_item("excel")

    def _on_folder_action_pdf_clicked(self) -> None:
        if time.monotonic() < float(getattr(self, "_folder_action_pdf_click_guard_until", 0.0)):
            return
        self._set_folder_action_inline_warning("")
        self._open_linked_asset_for_current_item("pdf")

    def _on_folder_action_pdf_2_clicked(self) -> None:
        if time.monotonic() < float(getattr(self, "_folder_action_pdf_click_guard_until", 0.0)):
            return
        self._set_folder_action_inline_warning("")
        self._open_linked_asset_for_current_item("pdf_2")

    def _on_folder_action_pdf_3_clicked(self) -> None:
        if time.monotonic() < float(getattr(self, "_folder_action_pdf_click_guard_until", 0.0)):
            return
        self._set_folder_action_inline_warning("")
        self._open_linked_asset_for_current_item("pdf_3")

    def _on_folder_action_pdf_4_clicked(self) -> None:
        if time.monotonic() < float(getattr(self, "_folder_action_pdf_click_guard_until", 0.0)):
            return
        self._set_folder_action_inline_warning("")
        self._open_linked_asset_for_current_item("pdf_4")

    def _on_folder_action_late_pdf_clicked(self) -> None:
        self._set_folder_action_inline_warning("")
        self._open_linked_asset_for_current_item("late_pdf")

    def _on_folder_action_orders_form_clicked(self) -> None:
        target = self._get_current_folder_action_target(action_name="Make Orders Form")
        if target is None:
            return
        item_id, db_item, folder_dir = target
        self._set_folder_action_inline_warning("")
        _path, _file_name, _meta_text, linked_exists = self._describe_asset_file(
            self._raw_asset_path_from_item(db_item, "orders_form")
        )
        if linked_exists:
            self._open_linked_asset_for_current_item("orders_form")
            return
        self._generate_orders_form_from_example(
            item_id=item_id,
            db_item=db_item,
            folder_dir=folder_dir,
            asset_kind="orders_form",
        )

    def _on_folder_action_orders_form_2_clicked(self) -> None:
        target = self._get_current_folder_action_target(action_name="Make Orders Form #2")
        if target is None:
            return
        item_id, db_item, folder_dir = target
        self._set_folder_action_inline_warning("")
        _path, _file_name, _meta_text, linked_exists = self._describe_asset_file(
            self._raw_asset_path_from_item(db_item, "orders_form_2")
        )
        if linked_exists:
            self._open_linked_asset_for_current_item("orders_form_2")
            return
        self._generate_orders_form_from_example(
            item_id=item_id,
            db_item=db_item,
            folder_dir=folder_dir,
            asset_kind="orders_form_2",
        )

    def _on_folder_action_orders_form_3_clicked(self) -> None:
        target = self._get_current_folder_action_target(action_name="Make Orders Form #3")
        if target is None:
            return
        item_id, db_item, folder_dir = target
        self._set_folder_action_inline_warning("")
        _path, _file_name, _meta_text, linked_exists = self._describe_asset_file(
            self._raw_asset_path_from_item(db_item, "orders_form_3")
        )
        if linked_exists:
            self._open_linked_asset_for_current_item("orders_form_3")
            return
        self._generate_orders_form_from_example(
            item_id=item_id,
            db_item=db_item,
            folder_dir=folder_dir,
            asset_kind="orders_form_3",
        )

    def _on_folder_action_orders_form_4_clicked(self) -> None:
        target = self._get_current_folder_action_target(action_name="Make Orders Form #4")
        if target is None:
            return
        item_id, db_item, folder_dir = target
        self._set_folder_action_inline_warning("")
        _path, _file_name, _meta_text, linked_exists = self._describe_asset_file(
            self._raw_asset_path_from_item(db_item, "orders_form_4")
        )
        if linked_exists:
            self._open_linked_asset_for_current_item("orders_form_4")
            return
        self._generate_orders_form_from_example(
            item_id=item_id,
            db_item=db_item,
            folder_dir=folder_dir,
            asset_kind="orders_form_4",
        )

    def _on_folder_action_qr_card_clicked(self, qr_kind: str) -> None:
        qr_kind = str(qr_kind or "").strip().lower()
        if qr_kind not in {"roster", "orders"}:
            return
        title = "QR Roster" if qr_kind == "roster" else "QR Orders"
        target = self._get_current_folder_action_target(action_name=title)
        if target is None:
            return
        item_id, db_item, _folder_dir = target
        if self._ensure_latest_orders_for_qr(int(item_id), qr_kind) is None:
            return
        try:
            refreshed = self.db.get_item_by_id(int(item_id))
            if refreshed:
                db_item = refreshed
                self.folder_action_anchor_db_item = refreshed
        except Exception:
            pass
        if self._linked_qr_is_stale(db_item, qr_kind):
            auto_summary = self._run_blocking_io_task(
                "Auto Update QR",
                (
                    f"{title} is older than its source PDF/Excel files.\n\n"
                    "Regenerating the QR output before opening it..."
                ),
                lambda: self._auto_regenerate_qr_for_item_ids([int(item_id)]),
            )
            if int(auto_summary.get("failed", 0) or 0) > 0:
                self._show_user_error_dialog(
                    "QR Update Blocked",
                    f"{title} is outdated, but DAMYComp could not regenerate it safely.",
                    next_steps=[
                        "Review the QR update errors.",
                        "Fix the linked PDF/Excel files if needed, then click the QR button again.",
                    ],
                    details="\n".join(str(x) for x in list(auto_summary.get("failure_lines") or [])[:20]),
                )
                return
            wanted = "QR Roster" if qr_kind == "roster" else "QR Orders"
            relevant_skips = [
                str(line)
                for line in list(auto_summary.get("skipped_lines") or [])
                if wanted in str(line)
            ]
            if relevant_skips:
                self._show_user_error_dialog(
                    "QR Update Blocked",
                    f"{title} is outdated, but DAMYComp could not regenerate it.",
                    next_steps=[
                        "Link the required source files for this QR output.",
                        "Then click the QR button again.",
                    ],
                    details="\n".join(relevant_skips[:20]),
                )
                return
            self._refresh_folder_action_asset_states(int(item_id))
            try:
                refreshed = self.db.get_item_by_id(int(item_id))
                if refreshed:
                    db_item = refreshed
                    self.folder_action_anchor_db_item = refreshed
            except Exception:
                pass
        asset_kind = "qr_roster" if qr_kind == "roster" else "qr_orders"
        _path, _file_name, _meta_text, linked_exists = self._describe_asset_file(
            self._raw_asset_path_from_item(db_item, asset_kind)
        )
        if linked_exists:
            self._set_folder_action_inline_warning("")
            self._open_linked_asset_for_current_item(asset_kind)
            return
        if qr_kind == "orders":
            self._set_folder_action_inline_warning(
                "QR Orders supports PDF-only mode. If no Excel is linked, DAMYComp will generate from PDF names."
            )
        else:
            self._set_folder_action_inline_warning(
                "QR format reminder: Excel must be child name / class / password in columns A/B/C. "
                "If class is blank, sheet name is used."
            )
        self._on_folder_action_qr_clicked(qr_kind)

    def _on_folder_action_qr_roster_clicked(self) -> None:
        self._on_folder_action_qr_card_clicked("roster")

    def _on_folder_action_qr_orders_clicked(self) -> None:
        self._on_folder_action_qr_card_clicked("orders")

    def _on_folder_action_excel_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("excel")

    def _on_folder_action_pdf_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("pdf")

    def _on_folder_action_pdf_2_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("pdf_2")

    def _on_folder_action_pdf_3_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("pdf_3")

    def _on_folder_action_pdf_4_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("pdf_4")

    def _on_folder_action_late_pdf_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("late_pdf")

    def _on_folder_action_orders_form_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("orders_form")

    def _on_folder_action_orders_form_2_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("orders_form_2")

    def _on_folder_action_orders_form_3_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("orders_form_3")

    def _on_folder_action_orders_form_4_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("orders_form_4")

    def _on_folder_action_qr_roster_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("qr_roster")

    def _on_folder_action_qr_orders_open_requested(self) -> None:
        self._open_linked_asset_for_current_item("qr_orders")

    def _on_folder_action_qr_clicked(self, qr_kind: str) -> None:
        qr_kind = str(qr_kind or "").strip().lower()
        title = "QR Roster" if qr_kind == "roster" else "QR Orders"
        operation_key = f"qr_{qr_kind}"
        target = self._get_current_folder_action_target(action_name=title)
        if target is None:
            return
        item_id, db_item, folder_dir = target

        excel_path: Path | None = None
        if qr_kind == "roster":
            excel_path = self._ensure_asset_path_for_qr(
                item_id=item_id,
                db_item=db_item,
                folder_dir=folder_dir,
                asset_kind="excel",
                title=title,
            )
            if excel_path is None:
                return
        else:
            raw_excel_value = str(getattr(db_item, "excel_path", "") or "").strip()
            if raw_excel_value:
                resolved_excel = self._resolve_action_path(raw_excel_value)
                if resolved_excel.exists():
                    excel_path = resolved_excel

        pdf_paths_for_qr = []
        if qr_kind == "orders":
            pdf_paths_for_qr = self._collect_linked_pdf_paths_for_qr(db_item)
            if not pdf_paths_for_qr:
                primary_pdf = self._ensure_asset_path_for_qr(
                    item_id=item_id,
                    db_item=db_item,
                    folder_dir=folder_dir,
                    asset_kind="pdf",
                    title=title,
                )
                if primary_pdf is None:
                    return
                pdf_paths_for_qr = self._collect_linked_pdf_paths_for_qr(db_item, preferred_first=primary_pdf)
        else:
            pdf_paths_for_qr = self._collect_linked_pdf_paths_for_qr(db_item)

        output_base_stem = excel_path.stem if excel_path is not None else folder_dir.name
        qr_output_stem = f"{output_base_stem}_qr_{qr_kind}"
        qr_output_pdf = (folder_dir / f"{qr_output_stem}.pdf").resolve()
        qr_output_manifest = (folder_dir / f"{qr_output_stem}.txt").resolve()
        existing_qr_targets = [p for p in (qr_output_pdf, qr_output_manifest) if p.exists()]
        if existing_qr_targets:
            action = self._ask_generate_conflict_action(
                title=title,
                existing_paths=existing_qr_targets,
            )
            if action == "cancel":
                self._set_completion_status(f"{title} cancelled.")
                return
            if action == "rename":
                qr_output_stem = self._next_available_generated_stem(
                    folder_dir,
                    qr_output_stem,
                    (".pdf", ".txt"),
                )

        if not self._try_begin_operation(operation_key, title):
            return

        try:
            qr_module = importlib.import_module("folder_manager.qr_tags_v1.main")
            result = self._run_blocking_io_task(
                title,
                f"{title} is running...\nPlease wait.",
                lambda: qr_module.generate_qr_tags(
                    excel_path=str(excel_path) if excel_path is not None else None,
                    pdf_path=str(pdf_paths_for_qr[0]) if pdf_paths_for_qr else None,
                    pdf_paths=[str(p) for p in pdf_paths_for_qr] if pdf_paths_for_qr else None,
                    output_dir=str(folder_dir),
                    mode=qr_kind,
                    output_base_name=qr_output_stem,
                ),
            )
        except Exception as exc:
            self._show_user_error_dialog(
                f"{title} Failed",
                f"DAMYComp could not finish {title}.",
                possible_causes=[
                    "The linked PDF or Excel file is not in the expected format.",
                    "A required QR dependency is missing from this DAMYComp install.",
                ],
                next_steps=[
                    "Verify the linked PDF and Excel files open normally.",
                    "If the files look correct, rebuild or reinstall DAMYComp with the QR dependencies.",
                ],
                details=str(exc),
            )
            self._end_operation(operation_key)
            return

        self._end_operation(operation_key)
        self._set_completion_status(f"{title} complete.")
        output_pdf = Path(str(result.output_pdf_path)).expanduser()
        if output_pdf.exists():
            try:
                if qr_kind == "roster":
                    self.db.set_qr_roster_path(item_id, str(output_pdf))
                else:
                    self.db.set_qr_orders_path(item_id, str(output_pdf))
                self._update_asset_marker_for_item(
                    item_id,
                    **self._asset_presence_flags_from_item(
                        db_item,
                        override={
                            "has_qr_roster": output_pdf.exists() if qr_kind == "roster" else None,
                            "has_qr_orders": output_pdf.exists() if qr_kind == "orders" else None,
                        },
                    ),
                )
            except Exception as exc:
                self._show_user_error_dialog(
                    f"Save {title} Path Failed",
                    f"DAMYComp generated {title} but could not save the output path to DB.",
                    possible_causes=[
                        "The database connection was interrupted.",
                        "The row was changed by another process.",
                    ],
                    next_steps=["Refresh the board and relink the generated file manually if needed."],
                    details=str(exc),
                )
        self._refresh_folder_action_asset_states(item_id)

        output_manifest = Path(str(result.manifest_path)).expanduser()
        pdf_status = "Created" if output_pdf.exists() else "Missing"
        manifest_status = "Created" if output_manifest.exists() else "Missing"
        summary_lines = []
        parsed_order_items = int(getattr(result, "parsed_order_items", 0) or 0)
        summary_lines.append(f"PDF orders: {parsed_order_items}")
        summary_lines.append(f"QR order tags: {int(result.matched_order_tags)}")
        unmatched_order_names = tuple(
            str(name or "").strip()
            for name in (getattr(result, "unmatched_pdf_names", tuple()) or tuple())
            if str(name or "").strip()
        )
        summary_lines.append(f"Unmatched: {len(unmatched_order_names)}")
        if unmatched_order_names:
            summary_lines.append(
                "Unmatched order names:\n" + "\n".join(f"- {name}" for name in unmatched_order_names)
            )
        summary_text = "\n\n".join(summary_lines)
        if output_pdf.exists():
            answer = self._ask_question_topmost_non_modal(
                title,
                summary_text,
                QMessageBox.Yes | QMessageBox.Ok,
                QMessageBox.Ok,
                button_labels={
                    int(QMessageBox.Yes): "Open PDF",
                    int(QMessageBox.Ok): "Done",
                },
                close_result=int(QMessageBox.Ok),
            )
            if answer == int(QMessageBox.Yes):
                try:
                    self._open_path_in_system(output_pdf)
                except Exception as exc:
                    self._show_user_error_dialog(
                        f"Open {title} Failed",
                        "DAMYComp generated the QR PDF but could not open it.",
                        possible_causes=[
                            "The PDF is locked by another program.",
                            "Windows does not have a working default PDF app.",
                        ],
                        next_steps=[
                            "Try opening the file manually from the folder.",
                            "Use the QR button again to open the saved PDF.",
                        ],
                        details=str(exc),
                    )
        else:
            self._show_info_text_dialog(
                title,
                summary_text,
                min_width=760,
                min_height=420,
            )

    def _safe_list_stage(self, stage: int):
        try:
            return self._run_db_operation_with_retry(
                f"list_by_stage({stage})",
                lambda: self.db.list_by_stage(stage),
                attempts=4,
                initial_delay_seconds=0.15,
            )
        except Exception as e:
            self._report_db_issue(
                "DB Error",
                f"Could not load stage {stage}.",
                e,
                suppress_transient_popup=True,
            )
            return []

    def _safe_list_all_by_stage(self) -> dict[int, list]:
        grouped: dict[int, list] = {}
        try:
            all_rows = self._run_db_operation_with_retry(
                "list_all(board_init)",
                lambda: self.db.list_all(),
                attempts=4,
                initial_delay_seconds=0.15,
            )
            for row in all_rows:
                try:
                    stage_id = int(getattr(row, "stage", 0))
                except Exception:
                    stage_id = 0
                grouped.setdefault(stage_id, []).append(row)
            return grouped
        except Exception as e:
            self._report_db_issue(
                "DB Error",
                "Could not load workflow board rows in one query; falling back to stage-by-stage loading.",
                e,
                suppress_transient_popup=True,
            )
            grouped.clear()
            for stage_def in STAGES:
                stage_id = int(stage_def.stage)
                grouped[stage_id] = self._safe_list_stage(stage_id)
            return grouped

    def _build_stage1_asset_scan_payload(
        self,
        items_by_stage: dict[int, list],
    ) -> list[tuple[int, str, str, str, str, str, str, str, str, str, str, str, str]]:
        payload: list[tuple[int, str, str, str, str, str, str, str, str, str, str, str, str]] = []
        stage_one_items = list(items_by_stage.get(1, []))
        for row in stage_one_items:
            try:
                item_id = int(getattr(row, "id", 0))
            except Exception:
                continue
            payload.append(
                (
                    item_id,
                    str(getattr(row, "orders_form_path", "") or "").strip(),
                    str(getattr(row, "pdf_path", "") or "").strip(),
                    str(getattr(row, "orders_form_path_2", "") or "").strip(),
                    str(getattr(row, "orders_form_path_3", "") or "").strip(),
                    str(getattr(row, "orders_form_path_4", "") or "").strip(),
                    str(getattr(row, "pdf_path_2", "") or "").strip(),
                    str(getattr(row, "pdf_path_3", "") or "").strip(),
                    str(getattr(row, "pdf_path_4", "") or "").strip(),
                    str(getattr(row, "late_pdf_path", "") or "").strip(),
                    str(getattr(row, "excel_path", "") or "").strip(),
                    str(getattr(row, "qr_roster_path", "") or "").strip(),
                    str(getattr(row, "qr_orders_path", "") or "").strip(),
                )
            )
        return payload

    def _compute_asset_scan_results(
        self,
        payload: list[tuple[int, str, str, str, str, str, str, str, str, str, str, str, str]],
    ) -> dict[int, dict[str, bool]]:
        results: dict[int, dict[str, bool]] = {}
        for (
            item_id,
            orders_path,
            pdf_path,
            orders_path_2,
            orders_path_3,
            orders_path_4,
            pdf_path_2,
            pdf_path_3,
            pdf_path_4,
            late_pdf_path,
            excel_path,
            qr_roster_path,
            qr_orders_path,
        ) in payload:
            results[int(item_id)] = {
                "has_orders_form": (
                    self._asset_link_exists(orders_path)
                    or self._asset_link_exists(orders_path_2)
                    or self._asset_link_exists(orders_path_3)
                    or self._asset_link_exists(orders_path_4)
                ),
                "has_pdf": (
                    self._asset_link_exists(pdf_path)
                    or self._asset_link_exists(pdf_path_2)
                    or self._asset_link_exists(pdf_path_3)
                    or self._asset_link_exists(pdf_path_4)
                ),
                "has_late_pdf": self._asset_link_exists(late_pdf_path),
                "has_excel": self._asset_link_exists(excel_path),
                "has_qr_roster": self._asset_link_exists(qr_roster_path),
                "has_qr_orders": self._asset_link_exists(qr_orders_path),
            }
        return results

    def _start_or_queue_asset_scan(
        self,
        payload: list[tuple[int, str, str, str, str, str, str, str, str, str, str, str, str]],
        *,
        generation: int,
    ) -> None:
        worker = self._asset_scan_worker
        if worker is not None and worker.isRunning():
            self._asset_scan_pending_payload = list(payload)
            self._asset_scan_pending_generation = int(generation)
            return
        self._start_asset_scan_worker(payload, generation=generation)

    def _start_asset_scan_worker(
        self,
        payload: list[tuple[int, str, str, str, str, str, str, str, str, str, str, str, str]],
        *,
        generation: int,
    ) -> None:
        if not payload:
            return
        worker = _IoTaskThread(lambda: self._compute_asset_scan_results(payload))
        self._asset_scan_worker = worker

        def _on_finished() -> None:
            self._on_asset_scan_worker_finished(worker, generation=int(generation))

        worker.finished.connect(_on_finished)
        worker.start()

    def _on_asset_scan_worker_finished(self, worker: _IoTaskThread, *, generation: int) -> None:
        if self._asset_scan_worker is worker:
            self._asset_scan_worker = None

        if worker.error is None:
            try:
                results = worker.result if isinstance(worker.result, dict) else {}
            except Exception:
                results = {}
            if int(generation) == int(self._asset_scan_generation_token):
                self._apply_asset_scan_results(results)

        pending_payload = self._asset_scan_pending_payload
        pending_generation = int(self._asset_scan_pending_generation or 0)
        self._asset_scan_pending_payload = None
        self._asset_scan_pending_generation = 0
        if pending_payload and pending_generation:
            self._start_asset_scan_worker(pending_payload, generation=pending_generation)

    def _apply_asset_scan_results(self, results: dict[int, dict[str, bool]]) -> None:
        if not results:
            return
        updated_lists: set[int] = set()
        for lw in self._iter_folder_list_widgets():
            if not bool(getattr(lw, "show_asset_markers", False)):
                continue
            for row in range(lw.count()):
                item = lw.item(row)
                if item is None:
                    continue
                raw_id = item.data(ROLE_DB_ID)
                try:
                    item_id = int(raw_id)
                except Exception:
                    continue
                flags = results.get(item_id)
                if not flags:
                    continue
                lw.apply_asset_presence_style(
                    item,
                    has_orders_form=bool(flags.get("has_orders_form")),
                    has_pdf=bool(flags.get("has_pdf")),
                    has_late_pdf=bool(flags.get("has_late_pdf")),
                    has_excel=bool(flags.get("has_excel")),
                    has_qr_roster=bool(flags.get("has_qr_roster")),
                    has_qr_orders=bool(flags.get("has_qr_orders")),
                )
                updated_lists.add(id(lw))
            if id(lw) in updated_lists:
                lw.viewport().update()

        anchor_id = self.folder_action_anchor_item_id
        if anchor_id is not None and int(anchor_id) in results:
            self._refresh_folder_action_asset_states(int(anchor_id))

    def _add_db_item_to_list(self, lw: DragListWidget, it):
        lw.add_entry(it.disk_name, it.display_name)
        last = lw.item(lw.count() - 1)
        last.setData(Qt.ItemDataRole.UserRole + 1, it.id)
        lw.apply_in_progress_style(last, it.in_progress_by)
        lw.apply_note_style(last, it.note)
        lw.apply_action_note_style(last, getattr(it, "action_note", None))
        lw.apply_contact_style(
            last,
            bool(
                str(getattr(it, "contact_name", "") or "").strip()
                or str(getattr(it, "contact_email", "") or "").strip()
                or str(getattr(it, "contact_phone", "") or "").strip()
            ),
        )
        lw.apply_note_color_style(last, it.note_color)
        show_asset_markers = bool(getattr(lw, "show_asset_markers", False))
        lw.apply_asset_presence_style(
            last,
            has_orders_form=(
                bool(str(it.orders_form_path or "").strip())
                or bool(str(getattr(it, "orders_form_path_2", "") or "").strip())
                or bool(str(getattr(it, "orders_form_path_3", "") or "").strip())
                or bool(str(getattr(it, "orders_form_path_4", "") or "").strip())
            ) if show_asset_markers else False,
            has_pdf=(
                bool(str(it.pdf_path or "").strip())
                or bool(str(getattr(it, "pdf_path_2", "") or "").strip())
                or bool(str(getattr(it, "pdf_path_3", "") or "").strip())
                or bool(str(getattr(it, "pdf_path_4", "") or "").strip())
            ) if show_asset_markers else False,
            has_late_pdf=bool(str(getattr(it, "late_pdf_path", "") or "").strip()) if show_asset_markers else False,
            has_excel=bool(str(it.excel_path or "").strip()) if show_asset_markers else False,
            has_qr_roster=bool(str(it.qr_roster_path or "").strip()) if show_asset_markers else False,
            has_qr_orders=bool(str(it.qr_orders_path or "").strip()) if show_asset_markers else False,
        )
        lw.apply_new_import_style(last, it.id in self.newly_added_item_ids)
        lw.apply_moved_style(last, it.id in self.moved_item_ids)
        lw.apply_updated_style(last, it.id in self.updated_item_ids)

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
        self.initialize_ui()

    def _find_duplicate_pid_groups(self) -> list[tuple[str, list[str]]]:
        rows = self._run_db_operation_with_retry(
            "find_duplicate_pids(precheck)",
            self._query_duplicate_pid_rows,
            attempts=5,
            initial_delay_seconds=0.2,
        )
        groups: dict[str, list[str]] = {}
        for raw_pid, raw_name in rows:
            pid = str(raw_pid or "").strip().upper()
            disk_name = str(raw_name or "").strip()
            if not pid or not disk_name:
                continue
            groups.setdefault(pid, []).append(disk_name)
        return [(pid, names) for pid, names in groups.items() if len(names) > 1]

    def _query_duplicate_pid_rows(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            source = self.db._workflow_read_source()
            cur.execute(
                f"""
                SELECT dup.pid, wi.disk_name
                FROM (
                    SELECT UPPER(BTRIM(pid)) AS pid
                    FROM {source}
                    WHERE NULLIF(BTRIM(COALESCE(pid, '')), '') IS NOT NULL
                      AND COALESCE(NULLIF(workflow_domain,''), %s) = %s
                    GROUP BY UPPER(BTRIM(pid))
                    HAVING COUNT(*) > 1
                ) AS dup
                JOIN {source} AS wi
                  ON UPPER(BTRIM(COALESCE(wi.pid, ''))) = dup.pid
                 AND COALESCE(NULLIF(wi.workflow_domain,''), %s) = %s
                ORDER BY dup.pid, LOWER(wi.disk_name)
                """,
                (
                    WORKFLOW_DOMAIN_PREPAID,
                    WORKFLOW_DOMAIN_PREPAID,
                    WORKFLOW_DOMAIN_PREPAID,
                    WORKFLOW_DOMAIN_PREPAID,
                ),
            )
            return list(cur.fetchall())

    def _pending_precheck_counts(self) -> tuple[int, int, int] | None:
        try:
            db_names = set(
                self._run_db_operation_with_retry(
                    "list_disk_names(refresh)",
                    lambda: self.db.list_disk_names(),
                    attempts=5,
                    initial_delay_seconds=0.2,
                )
            )
        except Exception as e:
            self._report_db_issue(
                "Refresh Failed",
                "Could not read DB rows.",
                e,
                suppress_transient_popup=True,
            )
            return None
        try:
            all_db_names = set(
                self._run_db_operation_with_retry(
                    "list_disk_names_all_domains(refresh)",
                    lambda: self.db.list_disk_names(domain=None),
                    attempts=5,
                    initial_delay_seconds=0.2,
                )
            )
        except Exception as e:
            self._report_db_issue(
                "Refresh Failed",
                "Could not read all workflow DB rows.",
                e,
                suppress_transient_popup=True,
            )
            return None

        try:
            duplicate_pid_groups = self._find_duplicate_pid_groups()
        except Exception as e:
            self._report_db_issue(
                "Refresh Failed",
                "Could not validate duplicate PIDs in DB rows.",
                e,
                suppress_transient_popup=True,
            )
            return None

        disk_names = self._list_sync_candidate_folders()
        db_candidate_names = {name for name in db_names if self._looks_like_workflow_folder_name(name)}
        disk_only_count = len(disk_names - all_db_names)
        db_only_count = len(db_candidate_names - disk_names)
        duplicate_pid_count = len(duplicate_pid_groups)
        return disk_only_count, db_only_count, duplicate_pid_count

    def _schedule_refresh_retry(self, delay_ms: int = 1500) -> None:
        if self._refresh_retry_timer.isActive():
            return
        if self._refresh_retry_remaining < 0:
            self._refresh_retry_remaining = 0
        next_attempt = max(1, 6 - int(self._refresh_retry_remaining))
        self._set_retry_status(
            f"Database connection is unstable. Retrying refresh in {max(1, int(delay_ms / 1000))} second(s) "
            f"(attempt {next_attempt}/5)..."
        )
        _append_ui_runtime_log(
            f"refresh auto-retry scheduled in {delay_ms}ms; remaining={self._refresh_retry_remaining}"
        )
        self._refresh_retry_timer.start(max(300, int(delay_ms)))

    def _retry_refresh_after_db_reconnect(self) -> None:
        self._run_refresh_flow(from_auto_retry=True)

    def _run_refresh_flow(self, *, from_auto_retry: bool) -> None:
        self._hide_folder_action_popup()
        if self._active_operations:
            if from_auto_retry:
                self._schedule_refresh_retry(1200)
                return
            self._focus_active_operation_ui()
            running = ", ".join(list(dict.fromkeys(self._active_operations.values())))
            self._show_info_message_dialog(
                "Program Running",
                f"{running} is running.\nPlease wait, then refresh again.",
                min_width=440,
                min_height=200,
            )
            return

        counts = self._pending_precheck_counts()
        if counts is None:
            if self._last_db_issue_transient:
                if self._refresh_retry_remaining <= 0:
                    if from_auto_retry:
                        _append_ui_runtime_log("refresh auto-retry exhausted")
                        self._set_retry_status("")
                        return
                    self._refresh_retry_remaining = 5
                self._refresh_retry_remaining -= 1
                if self._refresh_retry_remaining >= 0:
                    self._schedule_refresh_retry(1500)
            return

        if self._refresh_retry_timer.isActive():
            self._refresh_retry_timer.stop()
        self._set_retry_status("")
        self._refresh_retry_remaining = 0
        self._last_db_issue_transient = False
        disk_only_count, db_only_count, duplicate_pid_count = counts
        needs_precheck = bool(disk_only_count or db_only_count or duplicate_pid_count)

        if needs_precheck:
            ok = self._run_pre_import_sync_checks()
            if not ok:
                return

        self.reload_ui()
        if self._refresh_completion_pending:
            self._refresh_completion_pending = False
            self._set_completion_status("Refresh complete. Workflow board is up to date.")

    def _on_refresh_clicked(self) -> None:
        self._refresh_retry_remaining = 5
        self._refresh_completion_pending = True
        self._run_refresh_flow(from_auto_retry=False)

    def _fetch_visible_item_snapshot(self) -> dict[str, int]:
        snapshot: dict[str, int] = {}
        try:
            rows = self._run_db_operation_with_retry(
                "fetch_visible_item_snapshot",
                lambda: self._query_visible_item_snapshot_rows(),
                attempts=4,
                initial_delay_seconds=0.15,
            )
            for item_id, disk_name in rows:
                if disk_name:
                    snapshot[str(disk_name)] = int(item_id)
        except Exception:
            # Fallback to empty snapshot; UI still functions without new-item markers.
            return {}
        return snapshot

    def _query_visible_item_snapshot_rows(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            source = self.db._workflow_read_source()
            cur.execute(
                f"""
                SELECT id, disk_name
                FROM {source}
                WHERE COALESCE(NULLIF(workflow_domain,''), %s) = %s
                """,
                (WORKFLOW_DOMAIN_PREPAID, WORKFLOW_DOMAIN_PREPAID),
            )
            return list(cur.fetchall())

    def _compute_new_items_from_snapshot(self) -> int:
        before = self._calendar_import_before_snapshot or {}
        after = self._fetch_visible_item_snapshot()
        if not after:
            self.newly_added_item_ids = set()
            return 0

        new_ids = {item_id for disk_name, item_id in after.items() if disk_name not in before}
        # Keep pre-import sync "new" markers and merge with this import's new rows.
        self.newly_added_item_ids |= new_ids
        return len(new_ids)

    def _delete_db_item_by_disk_name(self, disk_name: str) -> None:
        try:
            item = self.db.get_item_by_disk_name(disk_name)
            if item:
                self.db.delete_item(item.id)
        except Exception as e:
            self._show_message_box_topmost_non_modal(
                QMessageBox.Icon.Warning,
                "DB Update Failed",
                f"Could not delete DB item '{disk_name}'.\n\n{e}",
            )

    def _rename_db_item_by_disk_name(self, old_disk_name: str, new_disk_name: str) -> None:
        if not new_disk_name or old_disk_name == new_disk_name:
            return
        try:
            item = self.db.get_item_by_disk_name(old_disk_name)
            if item:
                self.db.update_disk_name(item.id, new_disk_name)
        except Exception as e:
            self._show_message_box_topmost_non_modal(
                QMessageBox.Icon.Warning,
                "DB Update Failed",
                f"Could not rename DB item '{old_disk_name}' -> '{new_disk_name}'.\n\n{e}",
            )

    # --- MOVE logging happens here because drag_list_widget emits details ---
    def on_folder_dropped(self, item_id: int, old_stage: int, new_stage: int, disk_name: str):
        self.moved_item_ids.add(int(item_id))
        try:
            write_event(
                AuditEvent(
                    action="MOVE",
                    item_id=item_id,
                    disk_name=disk_name,
                    old_stage=old_stage,
                    new_stage=new_stage,
                ),
                base_dir=self.base_dir,
            )
        except Exception:
            pass

        # DragListWidget already emits uiReloadRequested, which drives a single reload.
        # Avoid reloading here to keep drag/drop responsive.

    def handle_i_checkbox(self, item):
        self.on_checkbox_change(item, "I")

    def handle_g_checkbox(self, item):
        self.on_checkbox_change(item, "G")

    def on_checkbox_change(self, item, label: str):
        if not self.edit_widgets:
            return

        lw_edit, lw_i, lw_g = self.edit_widgets
        index = lw_i.row(item) if self.sender() == lw_i else lw_g.row(item)
        if index < 0 or index >= lw_edit.count():
            return

        item.setText("✅" if item.checkState() == Qt.Checked else label)

        row_item = lw_edit.item(index)
        item_id = row_item.data(Qt.ItemDataRole.UserRole + 1)
        disk_name = row_item.data(Qt.ItemDataRole.UserRole) or row_item.text()
        if item_id is None:
            return
        item_id = int(item_id)

        try:
            new_flag_i = (lw_i.item(index).checkState() == Qt.Checked)
            new_flag_g = (lw_g.item(index).checkState() == Qt.Checked)
            self.db.update_flags(item_id, flag_i=new_flag_i, flag_g=new_flag_g)

            # LOG (must pass base_dir)
            if label == "I":
                write_event(
                    AuditEvent(
                        action="FLAGI",
                        item_id=item_id,
                        disk_name=disk_name,
                        value="ON" if new_flag_i else "OFF",
                    ),
                    base_dir=self.base_dir,
                )

            else:
                write_event(
                    AuditEvent(
                        action="FLAGG",
                        item_id=item_id,
                        disk_name=disk_name,
                        value="ON" if new_flag_g else "OFF",
                    ),
                    base_dir=self.base_dir,
                )


        except Exception as e:
            self._show_message_box_topmost_non_modal(
                QMessageBox.Icon.Warning,
                "DB Update Failed",
                f"Could not update flags for id={item_id}\n\n{e}",
            )

    def perform_search(self, text: str):
        self._hide_folder_action_popup()
        text = text.strip().lower()

        for col_widget, lw in self.column_widgets:
            any_visible = False

            if self.edit_widgets and lw == self.edit_widgets[0]:
                lw_edit, lw_i, lw_g = self.edit_widgets
                for i in range(lw_edit.count()):
                    row_item = lw_edit.item(i)
                    progress_text = str(row_item.data(Qt.ItemDataRole.UserRole + 2) or "").lower()
                    match = (not text) or (text in row_item.text().lower()) or (text in progress_text)
                    row_item.setHidden(not match)
                    if i < lw_i.count():
                        lw_i.item(i).setHidden(not match)
                    if i < lw_g.count():
                        lw_g.item(i).setHidden(not match)
                    if match:
                        any_visible = True
                col_widget.setVisible(any_visible or not text)
            else:
                for i in range(lw.count()):
                    row_item = lw.item(i)
                    progress_text = str(row_item.data(Qt.ItemDataRole.UserRole + 2) or "").lower()
                    match = (not text) or (text in row_item.text().lower()) or (text in progress_text)
                    row_item.setHidden(not match)
                    if match:
                        any_visible = True
                col_widget.setVisible(any_visible or not text)

        # No DB work needed here; search only toggles visibility.

    def _looks_like_workflow_folder_name(self, name: str) -> bool:
        s = (name or "").strip()
        if not s:
            return False
        if re.match(r"^\d{6}\b", s):
            return True
        for stage_def in STAGES:
            for prefix in stage_def.prefixes:
                if s.startswith(prefix):
                    return True
        return False

    def _list_sync_candidate_folders_from_dir(self, root_dir: str) -> set[str]:
        if not root_dir or not os.path.isdir(root_dir):
            return set()

        ignored_exact = {
            "cancel",
            "_workflow_log",
            "1. order form",
            "order form",
            "__pycache__",
        }
        folders: set[str] = set()
        for entry in os.scandir(root_dir):
            if not entry.is_dir():
                continue
            name = (entry.name or "").strip()
            lowered = name.lower()
            if not name or lowered in ignored_exact or name.startswith("."):
                continue
            if self._looks_like_workflow_folder_name(name):
                folders.add(name)
        return folders

    def _list_sync_candidate_folders(self) -> set[str]:
        # When a source fallback is configured, treat base + source as one logical pool.
        names = self._list_sync_candidate_folders_from_dir(self.base_dir)
        source_dir = (self.source_base_dir or "").strip()
        if source_dir and os.path.normcase(source_dir) != os.path.normcase(self.base_dir):
            names |= self._list_sync_candidate_folders_from_dir(source_dir)
        return names

    def _ask_stage_for_disk_only_folder(self, folder_name: str) -> tuple[str, int | None]:
        dlg = QDialog(self)
        dlg.setWindowTitle("Folder Exists, DB Missing")
        dlg.setModal(True)
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet(
            "QDialog { background: #20252e; color: #e8ecf1; }"
            "QLabel { color: #dbe3ee; }"
            "QComboBox { background: #151a21; color: #f4f7fb; border: 1px solid #3b4557; border-radius: 6px; padding: 6px; }"
            "QPushButton { padding: 8px 14px; border-radius: 8px; border: 1px solid #3b4557; background: #2b3340; color: #f4f7fb; }"
            "QPushButton:hover { background: #364255; }"
        )

        layout = QVBoxLayout(dlg)
        info_label = QLabel(
            "This folder already exists in DAMY, but it is missing from the workflow DB.\n\n"
            f"Folder: {folder_name}\n\n"
            "Choose which workflow column/stage it should go to.\n"
            "If you are not sure, keep the suggested stage and click Add to DB."
        )
        layout.addWidget(info_label)

        combo = QComboBox()
        suggested_stage = detect_stage_from_disk_name(folder_name)
        suggested_idx = 0
        for idx, stage_def in enumerate(STAGES):
            combo.addItem(f"{stage_def.stage}. {stage_def.label}", stage_def.stage)
            if stage_def.stage == suggested_stage:
                suggested_idx = idx
        combo.setCurrentIndex(suggested_idx)
        layout.addWidget(combo)

        row = QHBoxLayout()
        btn_ok = QPushButton("Add to DB")
        btn_ignore = QPushButton("Skip")
        btn_ignore_all = QPushButton("Skip All")
        row.addWidget(btn_ok)
        row.addWidget(btn_ignore)
        row.addWidget(btn_ignore_all)
        layout.addLayout(row)

        # Closing this dialog via X aborts all remaining pre-import flow.
        result = {"action": "abort_all", "stage": None}

        def _choose_ok():
            result["action"] = "ok"
            result["stage"] = int(combo.currentData())
            dlg.accept()

        def _choose_ignore():
            result["action"] = "ignore"
            dlg.accept()

        def _choose_ignore_all():
            result["action"] = "ignore_all"
            dlg.accept()

        btn_ok.clicked.connect(_choose_ok)
        btn_ignore.clicked.connect(_choose_ignore)
        btn_ignore_all.clicked.connect(_choose_ignore_all)
        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[info_label, combo],
            button_widgets=[btn_ok, btn_ignore, btn_ignore_all],
            base_width=700,
        )

        self._show_dialog_topmost_non_modal(dlg)
        return result["action"], result["stage"]

    def _ask_disk_only_map_qt(self, folder_names: list[str]) -> tuple[str, list[tuple[str, int]]]:
        dlg = QDialog(self)
        dlg.setWindowTitle("Add Missing DB Rows")
        dlg.setModal(True)
        dlg.setMinimumSize(980, 620)
        dlg.setStyleSheet(
            "QDialog { background: #20252e; color: #e8ecf1; }"
            "QLabel { color: #dbe3ee; }"
            "QLineEdit { min-height: 34px; }"
            "QTableWidget { background: #151a21; color: #f4f7fb; border: 1px solid #3b4557; border-radius: 8px; gridline-color: #303949; }"
            "QHeaderView::section { background: #2a3342; color: #e9edf5; padding: 6px; border: 1px solid #3b4557; }"
            "QComboBox { background: #151a21; color: #f4f7fb; border: 1px solid #3b4557; border-radius: 6px; padding: 4px 8px; min-height: 30px; }"
            "QPushButton { min-height: 34px; padding: 6px 12px; border-radius: 8px; border: 1px solid #3b4557; background: #2b3340; color: #f4f7fb; }"
            "QPushButton:hover { background: #364255; }"
        )

        stages = [(int(s.stage), f"{s.stage}. {s.label}") for s in STAGES]
        names = list(folder_names)
        states: dict[str, int] = {}
        for name in names:
            auto_stage = int(detect_stage_from_disk_name(name))
            states[name] = auto_stage

        layout = QVBoxLayout(dlg)
        layout.addWidget(
            QLabel(
                "These folders exist in DAMY but are missing from the workflow DB.\n\n"
                "Review the folder list below. Keep the suggested stage if it looks correct, or change it before applying."
            )
        )

        search_input = QLineEdit()
        search_input.setPlaceholderText("Search folder name...")
        layout.addWidget(search_input)

        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["Folder", "Stage"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, table.horizontalHeader().ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, table.horizontalHeader().ResizeMode.ResizeToContents)
        layout.addWidget(table)

        summary_label = QLabel("")
        layout.addWidget(summary_label)

        rebuilding = {"value": False}

        def _refresh_summary() -> None:
            selected_count = sum(1 for n in names if int(states[n]) > 0)
            summary_label.setText(f"Selected to add into DB: {selected_count} / {len(names)}")

        def _refresh_table() -> None:
            rebuilding["value"] = True
            try:
                needle = (search_input.text() or "").strip().lower()
                table.setRowCount(0)
                for name in names:
                    if needle and needle not in name.lower():
                        continue

                    row = table.rowCount()
                    table.insertRow(row)

                    folder_item = QTableWidgetItem(name)
                    has_prefix = self._has_explicit_stage_prefix(name)
                    if has_prefix:
                        folder_item.setToolTip("Auto classified by prefix (editable).")
                    else:
                        folder_item.setToolTip("No explicit stage prefix. Please choose a stage.")
                    table.setItem(row, 0, folder_item)

                    stage_combo = NoWheelComboBox()
                    stage_combo.addItem("Ignore", 0)
                    for stage_value, stage_label in stages:
                        stage_combo.addItem(stage_label, stage_value)
                    target_stage = int(states[name])
                    stage_idx = 0
                    for i in range(stage_combo.count()):
                        if int(stage_combo.itemData(i) or 0) == target_stage:
                            stage_idx = i
                            break
                    stage_combo.setCurrentIndex(stage_idx)
                    table.setCellWidget(row, 1, stage_combo)

                    stage_combo.currentIndexChanged.connect(
                        lambda _i, n=name, sc=stage_combo: (
                            states.__setitem__(n, int(sc.currentData() or 0)),
                            _refresh_summary(),
                        )
                    )
            finally:
                rebuilding["value"] = False
            _refresh_summary()

        tools_row = QHBoxLayout()
        btn_select_all_ok = QPushButton("Use Suggested Stage For All")
        btn_ignore_all = QPushButton("Set All Ignore")
        tools_row.addWidget(btn_select_all_ok)
        tools_row.addWidget(btn_ignore_all)
        tools_row.addStretch(1)
        layout.addLayout(tools_row)

        result = {"action": "back", "rows": []}
        action_row = QHBoxLayout()
        btn_apply = QPushButton("Apply Selected")
        btn_skip = QPushButton("Skip For Now")
        btn_cancel_import = QPushButton("Cancel Refresh")
        action_row.addWidget(btn_apply)
        action_row.addWidget(btn_skip)
        action_row.addWidget(btn_cancel_import)
        layout.addLayout(action_row)

        action_help = QLabel(
            "Apply Selected: add the checked folders into the workflow DB.\n"
            "Skip For Now: leave them unchanged and continue."
        )
        action_help.setWordWrap(True)
        layout.addWidget(action_help)

        def _set_all_decision(is_ok: bool) -> None:
            for n in names:
                if is_ok:
                    if int(states[n]) <= 0:
                        states[n] = int(detect_stage_from_disk_name(n))
                else:
                    states[n] = 0
            _refresh_table()

        def _apply() -> None:
            mapped_rows: list[tuple[str, int]] = []
            for n in names:
                stage_value = int(states[n])
                if stage_value <= 0:
                    continue
                mapped_rows.append((n, stage_value))
            result["action"] = "apply"
            result["rows"] = mapped_rows
            dlg.accept()

        def _cancel_import() -> None:
            result["action"] = "back"
            result["rows"] = []
            dlg.reject()

        def _skip_for_now() -> None:
            result["action"] = "skip_all"
            result["rows"] = []
            dlg.accept()

        search_input.textChanged.connect(lambda _text: _refresh_table())
        btn_select_all_ok.clicked.connect(lambda: _set_all_decision(True))
        btn_ignore_all.clicked.connect(lambda: _set_all_decision(False))
        btn_apply.clicked.connect(_apply)
        btn_skip.clicked.connect(_skip_for_now)
        btn_cancel_import.clicked.connect(_cancel_import)

        _refresh_table()
        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[search_input, table, summary_label],
            button_widgets=[btn_select_all_ok, btn_ignore_all, btn_apply, btn_skip, btn_cancel_import],
            base_width=980,
        )
        self._show_dialog_topmost_non_modal(dlg)
        return str(result["action"]), list(result["rows"])

    def _ask_db_only_create_map_qt(self, folder_names: list[str]) -> tuple[str, list[str]]:
        dlg = QDialog(self)
        dlg.setWindowTitle("Create Missing DAMY Folders")
        dlg.setModal(True)
        dlg.setMinimumSize(760, 520)
        dlg.setStyleSheet(
            "QDialog { background: #20252e; color: #e8ecf1; }"
            "QLabel { color: #dbe3ee; }"
            "QLineEdit { min-height: 34px; }"
            "QListWidget { background: #151a21; color: #f4f7fb; border: 1px solid #3b4557; border-radius: 8px; }"
            "QListWidget::item { height: 28px; }"
            "QPushButton { min-height: 34px; padding: 6px 12px; border-radius: 8px; border: 1px solid #3b4557; background: #2b3340; color: #f4f7fb; }"
            "QPushButton:hover { background: #364255; }"
        )

        layout = QVBoxLayout(dlg)
        layout.addWidget(
            QLabel(
                "These workflow rows exist in the DB but their folders are missing in DAMY.\n\n"
                "Review the list below, then decide whether to create the missing folders or remove the selected DB rows."
            )
        )

        search_input = QLineEdit()
        search_input.setPlaceholderText("Search folder name...")
        layout.addWidget(search_input)

        selected_states: dict[str, Qt.CheckState] = {name: Qt.CheckState.Checked for name in folder_names}
        all_names = list(folder_names)
        lw = QListWidget()
        lw.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(lw)

        selected_count_label = QLabel("")
        layout.addWidget(selected_count_label)

        rebuilding = {"value": False}

        def _refresh_count() -> None:
            selected_count = sum(1 for state in selected_states.values() if state == Qt.CheckState.Checked)
            selected_count_label.setText(f"Selected rows: {selected_count} / {len(all_names)}")

        def _refresh_list() -> None:
            rebuilding["value"] = True
            try:
                needle = (search_input.text() or "").strip().lower()
                lw.clear()
                for name in all_names:
                    if needle and needle not in name.lower():
                        continue
                    item = QListWidgetItem(name)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(selected_states.get(name, Qt.CheckState.Checked))
                    item.setData(Qt.ItemDataRole.UserRole, name)
                    lw.addItem(item)
            finally:
                rebuilding["value"] = False
            _refresh_count()

        def _on_item_changed(item: QListWidgetItem) -> None:
            if rebuilding["value"]:
                return
            name = item.data(Qt.ItemDataRole.UserRole) or item.text()
            selected_states[str(name)] = item.checkState()
            _refresh_count()

        def _set_all(checked: bool) -> None:
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for name in all_names:
                selected_states[name] = state
            _refresh_list()

        search_input.textChanged.connect(lambda _text: _refresh_list())
        lw.itemChanged.connect(_on_item_changed)
        _refresh_list()

        tools_row = QHBoxLayout()
        btn_select_all = QPushButton("Select All")
        btn_clear_all = QPushButton("Clear All")
        tools_row.addWidget(btn_select_all)
        tools_row.addWidget(btn_clear_all)
        tools_row.addStretch(1)
        layout.addLayout(tools_row)

        result = {"action": "back", "selected": []}

        action_row = QHBoxLayout()
        btn_create = QPushButton("Apply Selected")
        btn_delete = QPushButton("Delete Selected")
        btn_delete.setStyleSheet(
            "QPushButton { min-height: 34px; padding: 6px 12px; border-radius: 8px; "
            "border: 1px solid #8a4242; background: #5a2b2b; color: #f4f7fb; }"
            "QPushButton:hover { background: #744040; }"
        )
        btn_skip_all = QPushButton("Skip For Now")
        btn_cancel_import = QPushButton("Cancel Refresh")
        action_row.addWidget(btn_create)
        action_row.addWidget(btn_delete)
        action_row.addWidget(btn_skip_all)
        action_row.addWidget(btn_cancel_import)
        layout.addLayout(action_row)

        action_help = QLabel(
            "Apply Selected: create the checked folders in DAMY.\n"
            "Skip For Now: leave them unchanged and continue.\n"
            "Delete Selected: remove the checked rows from the workflow DB."
        )
        action_help.setWordWrap(True)
        layout.addWidget(action_help)

        def _choose_create() -> None:
            selected = [name for name in all_names if selected_states.get(name, Qt.CheckState.Checked) == Qt.CheckState.Checked]
            result["action"] = "create_selected"
            result["selected"] = selected
            dlg.accept()

        def _choose_delete_selected() -> None:
            selected = [name for name in all_names if selected_states.get(name, Qt.CheckState.Checked) == Qt.CheckState.Checked]
            result["action"] = "delete_selected"
            result["selected"] = selected
            dlg.accept()

        def _choose_skip_all() -> None:
            result["action"] = "skip_all"
            result["selected"] = []
            dlg.accept()

        def _choose_cancel_import() -> None:
            result["action"] = "back"
            result["selected"] = []
            dlg.reject()

        btn_select_all.clicked.connect(lambda: _set_all(True))
        btn_clear_all.clicked.connect(lambda: _set_all(False))
        btn_create.clicked.connect(_choose_create)
        btn_delete.clicked.connect(_choose_delete_selected)
        btn_skip_all.clicked.connect(_choose_skip_all)
        btn_cancel_import.clicked.connect(_choose_cancel_import)
        self._attach_responsive_font_scaling(
            dlg,
            text_widgets=[search_input, lw, selected_count_label],
            button_widgets=[btn_select_all, btn_clear_all, btn_create, btn_delete, btn_skip_all, btn_cancel_import],
            base_width=920,
        )

        self._show_dialog_topmost_non_modal(dlg)
        return str(result["action"]), list(result["selected"])

    def _show_precheck_overview(
        self,
        *,
        disk_only_count: int,
        db_only_count: int,
        duplicate_pid_count: int,
    ) -> bool:
        steps: list[str] = []
        step_no = 1
        if disk_only_count:
            steps.append(
                f"{step_no}. Add Missing DB Rows: review {disk_only_count} folder(s) that already exist in DAMY but are missing from the DB."
            )
            step_no += 1
        if db_only_count:
            steps.append(
                f"{step_no}. Create Missing DAMY Folders: review {db_only_count} DB row(s) that do not currently have a folder in DAMY."
            )
            step_no += 1
        if duplicate_pid_count:
            steps.append(
                f"{step_no}. Review Duplicate PIDs: check {duplicate_pid_count} PID group(s) that appear more than once in the workflow DB."
            )
        if not steps:
            return True

        message = (
            "Before refresh, DAMYComp found differences between the workflow DB and the DAMY folders.\n\n"
            "What will happen next:\n"
            f"{chr(10).join(steps)}\n\n"
            "You can fix each group now, or skip parts and continue.\n\n"
            "At the end, DAMYComp will show one summary of what was changed and what still needs attention."
        )
        answer = self._ask_question_topmost_non_modal(
            "Refresh Check Guide",
            message,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Yes,
            button_labels={
                int(QMessageBox.Yes): "Start Review",
                int(QMessageBox.Cancel): "Cancel Refresh",
            },
            close_result=int(QMessageBox.Cancel),
        )
        return answer == int(QMessageBox.Yes)

    def _run_pre_import_sync_checks(self) -> bool:
        try:
            db_names = set(
                self._run_db_operation_with_retry(
                    "list_disk_names(precheck)",
                    lambda: self.db.list_disk_names(),
                    attempts=5,
                    initial_delay_seconds=0.2,
                )
            )
        except Exception as e:
            self._report_db_issue(
                "Pre-Import Check Failed",
                "Could not read DB rows.",
                e,
                suppress_transient_popup=True,
            )
            return False
        try:
            all_db_names = set(
                self._run_db_operation_with_retry(
                    "list_disk_names_all_domains(precheck)",
                    lambda: self.db.list_disk_names(domain=None),
                    attempts=5,
                    initial_delay_seconds=0.2,
                )
            )
        except Exception as e:
            self._report_db_issue(
                "Pre-Import Check Failed",
                "Could not read all workflow DB rows.",
                e,
                suppress_transient_popup=True,
            )
            return False

        try:
            duplicate_pid_groups = self._find_duplicate_pid_groups()
        except Exception as e:
            self._report_db_issue(
                "Pre-Import Check Failed",
                "Could not validate duplicate PIDs in DB rows.",
                e,
                suppress_transient_popup=True,
            )
            return False

        disk_names = self._list_sync_candidate_folders()
        db_candidate_names = {name for name in db_names if self._looks_like_workflow_folder_name(name)}
        disk_only = sorted(disk_names - all_db_names, key=str.lower)
        db_only = sorted(db_candidate_names - disk_names, key=str.lower)

        if not self._show_precheck_overview(
            disk_only_count=len(disk_only),
            db_only_count=len(db_only),
            duplicate_pid_count=len(duplicate_pid_groups),
        ):
            return False

        summary_lines = [
            "Refresh review finished.",
            "",
            f"Folders on disk but missing from DB: {len(disk_only)}",
            f"DB rows without folders on disk: {len(db_only)}",
            f"Duplicate PID groups in DB: {len(duplicate_pid_groups)}",
        ]
        issue_sections: list[str] = []

        added_to_db = 0
        created_on_disk = 0
        deleted_from_db = 0
        precheck_new_ids: set[int] = set()

        if disk_only:
            action, mapped_rows = self._ask_disk_only_map_qt(disk_only)
            if action == "back":
                return False
            if action == "skip_all":
                mapped_rows = []
            upsert_errors: list[str] = []
            for folder_name, stage in mapped_rows:
                try:
                    new_id = self._upsert_from_disk_name_with_retry(folder_name, stage)
                    precheck_new_ids.add(int(new_id))
                    self.newly_added_item_ids.add(int(new_id))
                    added_to_db += 1
                except Exception as e:
                    detail = f"{folder_name}: {e}"
                    _append_ui_runtime_log(f"precheck db upsert failed: {detail}")
                    upsert_errors.append(detail)
            if upsert_errors:
                preview = "\n".join(upsert_errors[:80])
                if len(upsert_errors) > 80:
                    preview += f"\n...and {len(upsert_errors) - 80} more"
                issue_sections.append(
                    "Folders that could not be added to the workflow DB:\n"
                    f"Failed: {len(upsert_errors)} / {len(mapped_rows)}\n\n"
                    f"{preview}"
                )
        summary_lines.append(f"Folders added to DB: {added_to_db}")

        if db_only:
            action, selected_to_create = self._ask_db_only_create_map_qt(db_only)
            if action == "back":
                return False
            if action == "skip_all":
                selected_to_create = []
            elif action == "delete_selected":
                selected_to_delete = list(selected_to_create)
                if selected_to_delete:
                    confirm_delete = self._confirm_dangerous_action(
                        title="Confirm DB Delete",
                        happened=f"Delete {len(selected_to_delete)} selected DB row(s)?",
                        impacts=[
                            "These rows will be removed from the workflow DB.",
                            "No folders on disk will be deleted.",
                        ],
                        confirm_label="Delete Selected",
                        cancel_label="Keep Rows",
                    )
                    if confirm_delete:
                        delete_errors: list[str] = []
                        for folder_name in selected_to_delete:
                            try:
                                item = self.db.get_item_by_disk_name(folder_name)
                                if item:
                                    self.db.delete_item(item.id)
                                    deleted_from_db += 1
                            except Exception as e:
                                delete_errors.append(f"{folder_name}: {e}")
                        if delete_errors:
                            issue_sections.append(
                                "DB rows that could not be deleted:\n"
                                + "\n".join(delete_errors[:30])
                                + (f"\n...and {len(delete_errors) - 30} more" if len(delete_errors) > 30 else "")
                            )
                selected_to_create = []
            create_errors: list[str] = []
            for folder_name in selected_to_create:
                try:
                    self._create_folder_from_db_row(folder_name)
                    created_on_disk += 1
                except Exception as e:
                    create_errors.append(f"{folder_name}: {e}")
            if create_errors:
                issue_sections.append(
                    "Folders that could not be created on disk:\n"
                    + "\n".join(create_errors[:30])
                    + (f"\n...and {len(create_errors) - 30} more" if len(create_errors) > 30 else "")
                )
        summary_lines.append(f"Folders created on disk: {created_on_disk}")
        summary_lines.append(f"DB rows deleted: {deleted_from_db}")

        if duplicate_pid_groups:
            duplicate_lines: list[str] = []
            for pid, folder_names in duplicate_pid_groups:
                duplicate_lines.append(f"{pid} ({len(folder_names)} rows)")
                for folder_name in folder_names:
                    duplicate_lines.append(f"  {folder_name}")
                duplicate_lines.append("")
            issue_sections.append(
                "Duplicate PIDs found in the workflow DB:\n"
                + "\n".join(duplicate_lines).rstrip()
            )

        if added_to_db or created_on_disk or deleted_from_db:
            # Pre-import sync inserted rows should also show "new" marker in UI.
            self.newly_added_item_ids |= precheck_new_ids
            self.reload_ui()

        if disk_only or db_only or duplicate_pid_groups:
            message = "\n".join(summary_lines)
            if issue_sections:
                message += "\n\nItems that still need attention:\n\n" + "\n\n".join(issue_sections)
            self._show_info_text_dialog(
                "Refresh Summary",
                message,
                min_width=860,
                min_height=520,
            )

        return True

    def _extract_db_sqlstate(self, exc: Exception) -> str:
        value = getattr(exc, "sqlstate", None)
        if value:
            return str(value)
        diag = getattr(exc, "diag", None)
        if diag is not None:
            diag_state = getattr(diag, "sqlstate", None)
            if diag_state:
                return str(diag_state)
        return ""

    def _run_db_operation_with_retry(
        self,
        op_name: str,
        operation,
        *,
        attempts: int = 4,
        initial_delay_seconds: float = 0.2,
    ):
        last_error: Exception | None = None
        delay = max(0.05, float(initial_delay_seconds))
        total_attempts = max(1, int(attempts))
        for attempt in range(1, total_attempts + 1):
            try:
                result = operation()
                if attempt > 1:
                    self._set_retry_status("")
                return result
            except Exception as exc:
                last_error = exc
                transient = self._is_transient_db_error(exc)
                sqlstate = self._extract_db_sqlstate(exc) or "(none)"
                _append_ui_runtime_log(
                    f"db retry op={op_name} attempt={attempt}/{total_attempts} "
                    f"transient={transient} sqlstate={sqlstate} err={exc}"
                )
                if attempt < total_attempts and transient:
                    self._set_retry_status(
                        f"Database connection is unstable. Retrying now ({attempt}/{total_attempts - 1})..."
                    )
                    self._yield_ui()
                    time.sleep(delay)
                    delay = min(1.5, delay * 2.0)
                    continue
                break
        self._set_retry_status("")
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"DB operation failed without exception: {op_name}")

    def _report_db_issue(
        self,
        title: str,
        context_message: str,
        exc: Exception,
        *,
        suppress_transient_popup: bool = True,
    ) -> None:
        sqlstate = self._extract_db_sqlstate(exc) or "(none)"
        transient = self._is_transient_db_error(exc)
        self._last_db_issue_transient = bool(transient)
        _append_ui_runtime_log(
            f"db issue title={title!r} transient={transient} sqlstate={sqlstate} "
            f"context={context_message!r} err={exc}"
        )
        if suppress_transient_popup and transient:
            return
        possible_causes = [
            "The database server is offline or unreachable.",
            "The network connection dropped during the request.",
        ]
        if not transient:
            possible_causes.insert(0, "The workflow DB rejected this request.")
        self._show_user_error_dialog(
            title,
            context_message,
            possible_causes=possible_causes,
            next_steps=[
                "Wait a moment and try again.",
                "If the problem continues, check the DB server connection.",
            ],
            details=f"SQLSTATE: {sqlstate}\n{exc}",
        )

    def _is_transient_db_error(self, exc: Exception) -> bool:
        code = self._extract_db_sqlstate(exc)
        transient_codes = {
            "08000", "08001", "08003", "08006",  # connection errors
            "57P03",  # cannot_connect_now
            "53300",  # too_many_connections
            "40001",  # serialization_failure
            "40P01",  # deadlock_detected
            "57014",  # query_canceled / timeout
        }
        if code in transient_codes:
            return True
        msg = str(exc).lower()
        return (
            "timeout" in msg
            or "timed out" in msg
            or "could not connect" in msg
            or "connection" in msg and ("closed" in msg or "reset" in msg or "failed" in msg)
        )

    def _upsert_from_disk_name_with_retry(self, folder_name: str, stage: int, *, attempts: int = 3) -> int:
        raw = (folder_name or "").strip()
        if not raw:
            raise ValueError("folder name is empty")

        valid_stage_ids = {int(s.stage) for s in STAGES}
        target_stage = int(stage)
        if target_stage not in valid_stage_ids:
            target_stage = int(detect_stage_from_disk_name(raw))

        last_error: Exception | None = None
        delay = 0.15
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                return int(self.db.upsert_from_disk_name(raw, target_stage))
            except Exception as exc:
                last_error = exc
                sqlstate = self._extract_db_sqlstate(exc)

                # Constraint fallback: stage invalid under DB check; retry once with inferred stage.
                if sqlstate == "23514":
                    inferred_stage = int(detect_stage_from_disk_name(raw))
                    if inferred_stage != target_stage:
                        target_stage = inferred_stage
                        if attempt < attempts:
                            continue

                if attempt < attempts and self._is_transient_db_error(exc):
                    _append_ui_runtime_log(
                        f"precheck db upsert retry {attempt}/{attempts} folder={raw!r} "
                        f"stage={target_stage} sqlstate={sqlstate or '(none)'} err={exc}"
                    )
                    time.sleep(delay)
                    delay = min(1.2, delay * 2.0)
                    continue
                break

        if last_error is None:
            raise RuntimeError("DB upsert failed with unknown error")
        raise last_error

    def _has_explicit_stage_prefix(self, name: str) -> bool:
        s = (name or "").strip()
        if not s:
            return False
        for stage_def in STAGES:
            for prefix in stage_def.prefixes:
                if s.startswith(prefix):
                    return True
        return bool(re.match(r"^\s*\d+\.\s+", s))

    def _find_prefixed_stage_mismatches(self) -> list[tuple[int, str, int, int]]:
        rows: list[tuple[int, str, int, int]] = []
        try:
            fetched_rows = self._run_db_operation_with_retry(
                "find_prefixed_stage_mismatches",
                self._query_stage_mismatch_rows,
                attempts=5,
                initial_delay_seconds=0.2,
            )
            for item_id, disk_name, stage in fetched_rows:
                name = (disk_name or "").strip()
                if not name or not self._has_explicit_stage_prefix(name):
                    continue
                expected_stage = detect_stage_from_disk_name(name)
                current_stage = int(stage)
                if current_stage != expected_stage:
                    rows.append((int(item_id), name, current_stage, expected_stage))
        except Exception as e:
            self._report_db_issue(
                "Pre-Import Check Failed",
                "Could not validate stage-prefix consistency.",
                e,
                suppress_transient_popup=True,
            )
            return []
        return rows

    def _query_stage_mismatch_rows(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            source = self.db._workflow_read_source()
            cur.execute(
                f"""
                SELECT id, disk_name, stage
                FROM {source}
                WHERE COALESCE(NULLIF(workflow_domain,''), %s) = %s
                """,
                (WORKFLOW_DOMAIN_PREPAID, WORKFLOW_DOMAIN_PREPAID),
            )
            return list(cur.fetchall())

    def _create_folder_from_db_row(self, folder_name: str) -> None:
        """
        Materialize a DB row into the active filesystem base:
        - create folder
        - if DB note exists, write <folder_name>.txt with note content
        """
        target = os.path.join(self.base_dir, folder_name)
        os.makedirs(target, exist_ok=True)

        db_item = self.db.get_item_by_disk_name(folder_name)
        if not db_item:
            return

        note_text = (db_item.note or "").strip()
        if not note_text:
            return

        note_path = os.path.join(target, f"{folder_name}.txt")
        with open(note_path, "w", encoding="utf-8") as fh:
            fh.write(note_text + "\n")
