from __future__ import annotations

import re
from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from folder_manager.sms.child_info import (
    DEFAULT_PARENT_MMS_BODY,
    delete_child_record,
    import_contact_file,
    prepare_child_info_assets,
    read_child_records,
    send_ready_child_mms,
    send_ready_parent_email,
    summarize_records,
    validate_parent_email,
    validate_parent_phone,
    write_child_records,
)
from folder_manager.ui.cloudflare_dialogs import ensure_r2_settings
from folder_manager.ui.sms_dialogs import ensure_twilio_settings

TaskRunner = Callable[[str, str, Callable[[], object]], object]


def _split_user_error(error: object) -> tuple[str, str]:
    raw = str(error or "").strip()
    if not raw:
        return "The action failed.", ""
    marker = "Traceback (most recent call last):"
    if marker in raw:
        user_text = raw.split(marker, 1)[0].strip()
        return user_text or "The action failed.", raw
    return raw, ""


def _show_user_error(
    parent,
    title: str,
    *,
    what_happened: str,
    checked: str = "",
    next_step: str = "",
    technical_detail: str = "",
) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(f"What happened:\n{str(what_happened or '').strip() or 'The action failed.'}")
    info_parts = []
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


def _friendly_prepare_error(exc: Exception) -> str:
    detail = str(exc or "").strip()
    detail = detail.split("Traceback (most recent call last):", 1)[0].strip()

    missing_match = re.search(r"PDFs folder not found:\s*(.+)", detail, flags=re.IGNORECASE)
    if missing_match:
        missing_path = missing_match.group(1).strip()
        return (
            "Parent Delivery needs the Stage 3 PDFs, but the PDFs folder was not found.\n\n"
            f"Missing folder:\n{missing_path}\n\n"
            "Run Stage 3 Upload & PDFs again, then retry Parent Delivery."
        )

    empty_match = re.search(r"No PDF files found under:\s*(.+)", detail, flags=re.IGNORECASE)
    if empty_match:
        pdf_path = empty_match.group(1).strip()
        return (
            "Parent Delivery found the PDFs folder, but there are no PDF files inside it.\n\n"
            f"Checked folder:\n{pdf_path}\n\n"
            "Run Stage 3 Upload & PDFs again, then retry Parent Delivery."
        )

    if "Missing Cloudflare R2 settings" in detail:
        return (
            "Parent Delivery needs Cloudflare R2 settings before it can publish preview links.\n\n"
            f"{detail}"
        )

    return detail or "Parent Delivery could not be prepared. Check the Stage 3 PDFs, then try again."


class ParentDeliveryDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        job_folder: str,
        disk_name: str,
        db=None,
        workflow_item_id: Optional[int] = None,
        task_runner: Optional[TaskRunner] = None,
        start_mode: str = "info",
    ):
        super().__init__(parent)
        self.job_folder = str(job_folder)
        self.disk_name = str(disk_name or "").strip()
        self.db = db
        self.workflow_item_id = int(workflow_item_id or 0)
        self.task_runner = task_runner
        self.start_mode = str(start_mode or "info")
        self.records = []
        self._loading_table = False
        self.setWindowTitle("Parent Delivery")
        self.setModal(True)
        self.resize(1180, 760)
        self.setMinimumSize(980, 620)
        self.setStyleSheet(
            "QDialog { background-color: #2b2b2b; color: #f4f7fb; }"
            "QLabel { color: #f4f7fb; }"
            "QTableWidget { background-color: #1f2329; alternate-background-color: #252b33;"
            " gridline-color: #343b44; border: 1px solid #3a424c; border-radius: 6px; }"
            "QHeaderView::section { background-color: #313844; color: #f4f7fb; padding: 6px 8px;"
            " border: 0; border-right: 1px solid #3a424c; }"
            "QPushButton { min-height: 38px; padding: 6px 12px; border-radius: 8px;"
            " border: 1px solid #475364; background-color: #313844; color: #f4f7fb; }"
            "QPushButton:hover { background-color: #384354; }"
            "QPushButton:disabled { color: #7c8796; background-color: #262c34; border-color: #323943; }"
            "QPushButton#primaryAction { background-color: #2c6ad6; border-color: #4b84e3; font-weight: 700; }"
            "QPushButton#primaryAction:hover { background-color: #3a78e4; }"
            "QPushButton#secondaryAction { background-color: #2e3743; border-color: #4a5668; }"
            "QPushButton#toolAction { background-color: #252c35; border-color: #404b5a; color: #c7d5e0; }"
        )

        layout = QVBoxLayout(self)
        title = QLabel(self.disk_name or "Parent Delivery")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        subtitle = QLabel("Edit parent phone/email here, then send MMS or email from each child's proof page.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #c7d5e0;")
        layout.addWidget(subtitle)

        self.next_step_label = QLabel("")
        self.next_step_label.setWordWrap(True)
        self.next_step_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.next_step_label.setStyleSheet(
            "background-color: #1f2f44; border: 1px solid #335a8a; border-radius: 6px; padding: 8px 10px;"
        )
        layout.addWidget(self.next_step_label)

        self.path_label = QLabel("")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setStyleSheet("color: #9fb4c8;")
        layout.addWidget(self.path_label)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color: #c7d5e0;")
        layout.addWidget(self.summary_label)

        summary_frame = QFrame(self)
        summary_frame.setFrameShape(QFrame.Shape.StyledPanel)
        summary_frame.setStyleSheet(
            "QFrame { background-color: #252b33; border: 1px solid #3a424c; border-radius: 6px; }"
            "QLabel[role='metric_title'] { color: #9fb4c8; font-size: 11px; }"
            "QLabel[role='metric_value'] { font-size: 18px; font-weight: 700; }"
        )
        summary_layout = QGridLayout(summary_frame)
        summary_layout.setContentsMargins(10, 8, 10, 8)
        summary_layout.setHorizontalSpacing(18)
        summary_layout.setVerticalSpacing(6)
        self.metric_labels: Dict[str, QLabel] = {}
        metric_titles = [
            ("mms_ready", "MMS Ready"),
            ("email_ready", "Email Ready"),
            ("missing_phone", "Missing Phone"),
            ("missing_email", "Missing Email"),
            ("needs_review", "Needs Review"),
            ("failed", "Failed"),
        ]
        for col, (key, label_text) in enumerate(metric_titles):
            title_label = QLabel(label_text)
            title_label.setProperty("role", "metric_title")
            value_label = QLabel("0")
            value_label.setProperty("role", "metric_value")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            summary_layout.addWidget(title_label, 0, col, alignment=Qt.AlignmentFlag.AlignCenter)
            summary_layout.addWidget(value_label, 1, col, alignment=Qt.AlignmentFlag.AlignCenter)
            self.metric_labels[key] = value_label
        layout.addWidget(summary_frame)

        self.table = QTableWidget(0, 7, self)
        self.table.setHorizontalHeaderLabels([
            "Child Name",
            "Class",
            "Parent Phone",
            "Parent Email",
            "MMS",
            "Email",
            "Note",
        ])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.SelectedClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.AnyKeyPressed
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemChanged.connect(self._table_item_changed)
        layout.addWidget(self.table, 1)

        button_row = QHBoxLayout()
        self.import_button = QPushButton("Import Parent Contacts", self)
        self.refresh_button = QPushButton("Refresh Status", self)
        self.send_mms_button = QPushButton("Send Ready MMS", self)
        self.send_email_button = QPushButton("Send Ready Email", self)
        self.close_button = QPushButton("Close", self)
        for button in (
            self.import_button,
            self.refresh_button,
            self.send_mms_button,
            self.send_email_button,
            self.close_button,
        ):
            button_row.addWidget(button)
        layout.addLayout(button_row)

        self.import_button.clicked.connect(self.import_contacts)
        self.refresh_button.clicked.connect(self.refresh_records)
        self.send_mms_button.clicked.connect(self.send_ready_mms)
        self.send_email_button.clicked.connect(self.send_ready_email)
        self.close_button.clicked.connect(self.accept)

        self.import_button.setObjectName("secondaryAction")
        self.refresh_button.setObjectName("toolAction")
        self.send_mms_button.setObjectName("primaryAction")
        self.send_email_button.setObjectName("primaryAction")
        self.close_button.setObjectName("toolAction")

        self.import_button.setToolTip(
            "Read parent phone numbers and email addresses from Excel, CSV, TXT, or PDF. "
            "If the parent list is missing, the app builds it from the Stage 3 PDFs automatically."
        )
        self.refresh_button.setToolTip("Reload parent contact and send status from the database.")
        self.send_mms_button.setToolTip("Send MMS only to rows marked Ready in the MMS column.")
        self.send_email_button.setToolTip("Send email only to rows marked Ready in the Email column.")
        self._ensure_child_info_exists("Parent Delivery")
        self.refresh_records()

    def _run_task(self, title: str, message: str, task: Callable[[], object]) -> object:
        if self.task_runner is not None:
            return self.task_runner(title, message, task)
        return task()

    def _sort_priority(self, record) -> tuple[int, str, str]:
        statuses = {str(record.mms_status or "").strip(), str(record.email_status or "").strip()}
        if "Needs review" in statuses:
            priority = 0
        elif "Failed" in statuses:
            priority = 1
        elif "Ready" in statuses:
            priority = 2
        elif "Missing" in statuses:
            priority = 3
        else:
            priority = 4
        return (priority, str(record.class_name or "").casefold(), str(record.child_name or "").casefold())

    def _read_records(self):
        return read_child_records(
            self.job_folder,
            db=self.db,
            workflow_item_id=self.workflow_item_id,
        )

    def _write_records(self) -> None:
        write_child_records(
            self.job_folder,
            self.records,
            db=self.db,
            workflow_item_id=self.workflow_item_id,
            disk_name=self.disk_name,
        )

    def _selected_row_index(self) -> int:
        rows = [index.row() for index in self.table.selectionModel().selectedRows()]
        if not rows:
            return -1
        return rows[0]

    def _selected_record(self):
        row = self._selected_row_index()
        if row < 0 or row >= len(self.records):
            return None
        return self.records[row]

    def refresh_records(self) -> None:
        records = sorted(self._read_records(), key=self._sort_priority)
        self.records = records
        summary = summarize_records(records)

        for key, label in self.metric_labels.items():
            label.setText(str(summary.get(key, 0)))
        self._apply_metric_styles(summary)
        self.path_label.setText("Parent contacts are saved in the database. Edit Parent Phone, Parent Email, and Note directly in this table.")
        self.summary_label.setText(
            f"{summary.get('total', 0)} children • MMS sent {summary.get('mms_sent', 0)} • Email sent {summary.get('email_sent', 0)}"
        )
        message, level = self._build_next_step_message(summary, bool(records))
        self.next_step_label.setText(message)
        self._apply_next_step_style(level)

        self.import_button.setEnabled(True)
        self.send_mms_button.setEnabled(summary["mms_ready"] > 0)
        self.send_email_button.setEnabled(summary["email_ready"] > 0)
        self.send_mms_button.setText(f"Send Ready MMS ({summary['mms_ready']})")
        self.send_email_button.setText(f"Send Ready Email ({summary['email_ready']})")
        self._apply_button_roles(summary, bool(records))

        self._loading_table = True
        self.table.setRowCount(len(records))
        for row_index, record in enumerate(records):
            values = [
                record.child_name,
                record.class_name,
                record.parent_phone,
                record.parent_email,
                record.mms_status,
                record.email_status,
                record.note,
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if col_index not in {2, 3, 6}:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col_index in {4, 5}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self._apply_status_style(item, str(value or ""))
                elif col_index == 6 and record.note:
                    item.setToolTip(record.note)
                elif col_index in {2, 3} and value:
                    item.setToolTip(str(value))
                self.table.setItem(row_index, col_index, item)
            self.table.setRowHeight(row_index, 24)
        self._loading_table = False

    def _table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table:
            return
        row = item.row()
        col = item.column()
        if col not in {2, 3, 6} or row < 0 or row >= len(self.records):
            return
        record = self.records[row]
        raw_value = str(item.text() or "").strip()
        try:
            if col == 2:
                new_value = validate_parent_phone(raw_value)
                if new_value != record.parent_phone:
                    record.mms_sid = ""
                    record.mms_sent_at = ""
                    record.mms_error = ""
                record.parent_phone = new_value
            elif col == 3:
                new_value = validate_parent_email(raw_value)
                if new_value != record.parent_email:
                    record.email_message_id = ""
                    record.email_sent_at = ""
                    record.email_error = ""
                record.parent_email = new_value
            else:
                record.note = raw_value
            self._write_records()
        except Exception as exc:
            user_error, technical_detail = _split_user_error(exc)
            previous = record.parent_phone if col == 2 else record.parent_email if col == 3 else record.note
            self._loading_table = True
            item.setText(previous or "")
            self._loading_table = False
            _show_user_error(
                self,
                "Invalid Parent Contact",
                what_happened=user_error,
                checked=raw_value,
                next_step="Enter a valid parent phone or email, then try again.",
                technical_detail=technical_detail,
            )
            return
        self.refresh_records()

    def _build_next_step_message(self, summary: Dict[str, int], records_exist: bool) -> tuple[str, str]:
        if not records_exist:
            return (
                "No parent delivery rows yet. The app builds rows from Stage 3 PDFs automatically; if that failed, check Cloudflare settings and Stage 3 PDFs.",
                "info",
            )
        if summary["needs_review"] > 0:
            return (
                f"Review needed: {summary['needs_review']} row(s) need attention. Fix Parent Phone, Parent Email, or Note directly in the table.",
                "warning",
            )
        if summary["failed"] > 0:
            return (
                f"Delivery issue: {summary['failed']} row(s) failed. Check the Note column, fix the problem, then try again.",
                "danger",
            )
        if summary["mms_ready"] > 0 or summary["email_ready"] > 0:
            return (
                f"Ready now: MMS {summary['mms_ready']} row(s), Email {summary['email_ready']} row(s). Send the ready rows.",
                "success",
            )
        if summary["missing_phone"] > 0 or summary["missing_email"] > 0:
            return (
                f"Waiting for contacts: {summary['missing_phone']} row(s) are missing phone, "
                f"{summary['missing_email']} row(s) are missing email. Import contacts or type them directly in the table.",
                "info",
            )
        if summary["total"] > 0:
            return (
                "Complete: no ready rows remain. Parent delivery is either sent already or waiting for new contact changes.",
                "complete",
            )
        return ("No child rows were found yet. Stage 3 must create PDFs before parent delivery can start.", "muted")

    def _apply_next_step_style(self, level: str) -> None:
        styles = {
            "info": "background-color: #1f2f44; border: 1px solid #335a8a; color: #d9e8fb;",
            "warning": "background-color: #43361b; border: 1px solid #8c6a1f; color: #ffe19a;",
            "danger": "background-color: #4b2020; border: 1px solid #9a4040; color: #ffbbbb;",
            "success": "background-color: #183822; border: 1px solid #2d7a46; color: #bdf3cb;",
            "complete": "background-color: #17324b; border: 1px solid #35658f; color: #b8dbff;",
            "muted": "background-color: #2a3139; border: 1px solid #475364; color: #d7dde5;",
        }
        self.next_step_label.setStyleSheet(
            f"{styles.get(level, styles['muted'])} border-radius: 6px; padding: 8px 10px;"
        )

    def _apply_metric_styles(self, summary: Dict[str, int]) -> None:
        styles = {
            "mms_ready": "#9ff0b4",
            "email_ready": "#9ac7ff",
            "missing_phone": "#d7dde5",
            "missing_email": "#d7dde5",
            "needs_review": "#ffd983",
            "failed": "#ffaaaa",
        }
        for key, label in self.metric_labels.items():
            active = summary.get(key, 0) > 0
            color = styles.get(key, "#f4f7fb") if active else "#7f8a98"
            label.setStyleSheet(f"color: {color};")

    def _apply_button_roles(self, summary: Dict[str, int], records_exist: bool) -> None:
        for button in (self.import_button, self.send_mms_button, self.send_email_button):
            button.setObjectName("secondaryAction")
        if summary["mms_ready"] > 0:
            self.send_mms_button.setObjectName("primaryAction")
        if summary["email_ready"] > 0:
            self.send_email_button.setObjectName("primaryAction")
        if summary["mms_ready"] <= 0 and summary["email_ready"] <= 0:
            self.import_button.setObjectName("primaryAction" if (not records_exist or summary["missing_phone"] > 0 or summary["missing_email"] > 0) else "secondaryAction")
        for button in (
            self.import_button,
            self.refresh_button,
            self.send_mms_button,
            self.send_email_button,
            self.close_button,
        ):
            button.style().unpolish(button)
            button.style().polish(button)

    def delete_selected_row(self) -> None:
        record = self._selected_record()
        if record is None:
            QMessageBox.information(self, "Delete Row", "Select one child row first.")
            return
        answer = QMessageBox.question(
            self,
            "Delete Parent Delivery Row",
            (
                f"Delete this child row from Parent Delivery?\n\n"
                f"Child: {record.child_name}\n"
                f"Class: {record.class_name}\n\n"
                "This does not delete the original PDF or uploaded Cloudflare image."
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted = delete_child_record(
                self.job_folder,
                record,
                db=self.db,
                workflow_item_id=self.workflow_item_id,
                disk_name=self.disk_name,
            )
        except Exception as exc:
            user_error, technical_detail = _split_user_error(exc)
            _show_user_error(
                self,
                "Delete Row",
                what_happened=f"Delete failed: {user_error}",
                checked=f"Child: {record.child_name}\nClass: {record.class_name}",
                next_step="Refresh Parent Delivery, then try deleting the row again.",
                technical_detail=technical_detail,
            )
            return
        if not deleted:
            _show_user_error(
                self,
                "Delete Row",
                what_happened="The selected row could not be found.",
                checked=f"Child: {record.child_name}\nClass: {record.class_name}",
                next_step="Refresh Parent Delivery and check whether the row was already removed.",
            )
        self.refresh_records()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete and self.table.hasFocus():
            if self.table.state() == QAbstractItemView.State.EditingState:
                super().keyPressEvent(event)
                return
            current_item = self.table.currentItem()
            if current_item is not None and current_item.column() in {2, 3, 6}:
                self._loading_table = True
                current_item.setText("")
                self._loading_table = False
                self._table_item_changed(current_item)
                event.accept()
                return
            self.delete_selected_row()
            event.accept()
            return
        super().keyPressEvent(event)

    def _ensure_child_info_exists(self, action_title: str) -> bool:
        if self._read_records():
            return True
        settings = ensure_r2_settings(self)
        if settings is None:
            return False
        try:
            self._run_task(
                "Prepare Parent Delivery",
                "Parent delivery rows are missing. Building them from the Stage 3 PDFs and publishing cloud preview links...\nPlease wait.",
                lambda: prepare_child_info_assets(
                    self.job_folder,
                    settings,
                    db=self.db,
                    workflow_item_id=self.workflow_item_id,
                    disk_name=self.disk_name,
                ),
            )
        except Exception as exc:
            user_error, technical_detail = _split_user_error(exc)
            _show_user_error(
                self,
                action_title,
                what_happened=_friendly_prepare_error(exc),
                checked=self.job_folder,
                next_step="Fix the issue above, then open Parent Delivery again.",
                technical_detail=technical_detail or user_error,
            )
            return False
        self.refresh_records()
        return bool(self._read_records())

    def _apply_status_style(self, item: QTableWidgetItem, status: str) -> None:
        status_map = {
            "Ready": (QColor("#113b1f"), QColor("#9ff0b4")),
            "Sent": (QColor("#16314f"), QColor("#9ac7ff")),
            "Needs review": (QColor("#4b3a12"), QColor("#ffd983")),
            "Failed": (QColor("#4c1f1f"), QColor("#ffaaaa")),
            "Missing": (QColor("#363b42"), QColor("#d7dde5")),
        }
        background, foreground = status_map.get(status, (QColor("#2d333b"), QColor("#e6edf3")))
        item.setBackground(background)
        item.setForeground(foreground)
        item.setToolTip(status)

    def import_contacts(self) -> None:
        if not self._ensure_child_info_exists("Import Parent Contacts"):
            return

        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose Contact File",
            self.job_folder,
            "Contact files (*.xlsx *.xlsm *.csv *.txt *.pdf);;All files (*.*)",
        )
        if not path:
            return
        try:
            result = self._run_task(
                "Import Parent Contacts",
                "Reading contact file and matching parent phone numbers and email addresses to children...\nPlease wait.",
                lambda: import_contact_file(
                    self.job_folder,
                    path,
                    overwrite=False,
                    db=self.db,
                    workflow_item_id=self.workflow_item_id,
                    disk_name=self.disk_name,
                ),
            )
        except Exception as exc:
            user_error, technical_detail = _split_user_error(exc)
            _show_user_error(
                self,
                "Import Parent Contacts",
                what_happened=f"Import failed: {user_error}",
                checked=path,
                next_step="Check the contact file format, then import again.",
                technical_detail=technical_detail,
            )
            return
        self.refresh_records()
        result_map = result if isinstance(result, dict) else {}
        unmatched_names = result_map.get("unmatched_names") or []
        unmatched_text = ""
        if unmatched_names:
            unmatched_text = "\nUnmatched names:\n- " + "\n- ".join(str(name) for name in unmatched_names)
        QMessageBox.information(
            self,
            "Import Parent Contacts",
            "Contact import complete.\n"
            f"Contacts found: {result_map.get('contacts_found', 0)}\n"
            f"Matched rows: {result_map.get('matched', 0)}\n"
            f"Phone updates: {result_map.get('phone_updated', 0)}\n"
            f"Email updates: {result_map.get('email_updated', 0)}\n"
            f"Needs review: {result_map.get('needs_review', 0)}\n"
            f"Unmatched: {result_map.get('unmatched', 0)}\n"
            f"Skipped existing values: {result_map.get('skipped_existing', 0)}\n\n"
            "Next step: review the editable table, then send ready rows."
            f"{unmatched_text}",
        )

    def send_ready_mms(self) -> None:
        if not self._ensure_child_info_exists("Send Parent MMS"):
            return
        records = self._read_records()
        summary = summarize_records(records)
        if summary["mms_ready"] <= 0:
            QMessageBox.information(
                self,
                "Send Parent MMS",
                "No rows are ready for MMS.\n\nAdd parent phone numbers or fix rows that need review, then refresh.",
            )
            return
        twilio_settings = ensure_twilio_settings(self)
        if twilio_settings is None:
            return
        answer = QMessageBox.question(
            self,
            "Send Parent MMS",
            f"Send MMS to {summary['mms_ready']} ready parent phone(s)?\n\nMessage:\n{DEFAULT_PARENT_MMS_BODY}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self._run_task(
                "Send Parent MMS",
                "Sending MMS messages through Twilio...\nPlease wait.",
                lambda: send_ready_child_mms(
                    self.job_folder,
                    twilio_settings,
                    db=self.db,
                    workflow_item_id=self.workflow_item_id,
                    disk_name=self.disk_name,
                ),
            )
        except Exception as exc:
            user_error, technical_detail = _split_user_error(exc)
            _show_user_error(
                self,
                "Send Parent MMS",
                what_happened=f"MMS send failed: {user_error}",
                checked=f"Ready rows: {summary['mms_ready']}",
                next_step="Check Twilio settings and parent phone numbers, then send ready MMS again.",
                technical_detail=technical_detail,
            )
            return
        self.refresh_records()
        result_map = result if isinstance(result, dict) else {}
        QMessageBox.information(
            self,
            "Send Parent MMS",
            "MMS send complete.\n"
            f"Sent: {result_map.get('sent', 0)}\n"
            f"Failed: {result_map.get('failed', 0)}\n"
            f"Skipped: {result_map.get('skipped', 0)}\n\n"
            "Check the MMS and Note columns for any rows that need follow-up.",
        )

    def send_ready_email(self) -> None:
        if not self._ensure_child_info_exists("Send Parent Email"):
            return
        records = self._read_records()
        summary = summarize_records(records)
        if summary["email_ready"] <= 0:
            QMessageBox.information(
                self,
                "Send Parent Email",
                "No rows are ready for email.\n\nAdd parent email addresses or fix rows that need review, then refresh.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Send Parent Email",
            f"Send email to {summary['email_ready']} ready parent email(s)?\n\n"
            "Each email includes the order button, password, and inline preview image.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self._run_task(
                "Send Parent Email",
                "Sending parent emails through Gmail with the order button, password, and inline preview image...\nPlease wait.",
                lambda: send_ready_parent_email(
                    self.job_folder,
                    db=self.db,
                    workflow_item_id=self.workflow_item_id,
                    disk_name=self.disk_name,
                ),
            )
        except Exception as exc:
            user_error, technical_detail = _split_user_error(exc)
            _show_user_error(
                self,
                "Send Parent Email",
                what_happened=f"Email send failed: {user_error}",
                checked=f"Ready rows: {summary['email_ready']}",
                next_step="Check Gmail authorization and parent email addresses, then send ready email again.",
                technical_detail=technical_detail,
            )
            return
        self.refresh_records()
        result_map = result if isinstance(result, dict) else {}
        QMessageBox.information(
            self,
            "Send Parent Email",
            "Email send complete.\n"
            f"Sent: {result_map.get('sent', 0)}\n"
            f"Failed: {result_map.get('failed', 0)}\n"
            f"Skipped: {result_map.get('skipped', 0)}\n\n"
            "Check the Email and Note columns for any rows that need follow-up.",
        )


ChildMmsDialog = ParentDeliveryDialog
