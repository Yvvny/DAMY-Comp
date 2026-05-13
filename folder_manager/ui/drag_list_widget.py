# drag_list_widget.py  (DB-backed: stage move only; disk folders unchanged)

import os
import re
import sys
import subprocess
from pathlib import Path
from typing import Optional


from PySide6.QtWidgets import (
    QApplication, QListWidget, QListWidgetItem, QMenu, QInputDialog,
    QAbstractItemView,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem, QColorDialog,
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTextEdit, QMessageBox,
    QWidget, QLineEdit
)
from PySide6.QtGui import (
    QColor,
    QDropEvent,
    QDragEnterEvent,
    QPainter,
    QPalette,
    QBrush,
    QTextOption,
    QTextCharFormat,
    QFont,
)
from PySide6.QtCore import Qt, Signal

from .dialog_system import show_information_unified, show_warning_unified
from ..workflow_logger import AuditEvent, write_event
from ..db import parse_contact_fields_from_note
ROLE_DISK_NAME = Qt.ItemDataRole.UserRole
ROLE_DB_ID = Qt.ItemDataRole.UserRole + 1
ROLE_IN_PROGRESS = Qt.ItemDataRole.UserRole + 2
ROLE_NOTE = Qt.ItemDataRole.UserRole + 3
ROLE_NOTE_COLOR = Qt.ItemDataRole.UserRole + 4
ROLE_IS_NEW = Qt.ItemDataRole.UserRole + 5
ROLE_IS_MOVED = Qt.ItemDataRole.UserRole + 6
ROLE_IS_UPDATED = Qt.ItemDataRole.UserRole + 7
ROLE_HAS_PDF = Qt.ItemDataRole.UserRole + 8
ROLE_HAS_EXCEL = Qt.ItemDataRole.UserRole + 9
ROLE_HAS_ORDERS_FORM = Qt.ItemDataRole.UserRole + 10
ROLE_HAS_QR_ROSTER = Qt.ItemDataRole.UserRole + 11
ROLE_HAS_QR_ORDERS = Qt.ItemDataRole.UserRole + 12
ROLE_ACTION_NOTE = Qt.ItemDataRole.UserRole + 13
ROLE_HAS_CONTACT = Qt.ItemDataRole.UserRole + 14
ROLE_HAS_LATE_PDF = Qt.ItemDataRole.UserRole + 15


class InProgressPrefixDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        selected = bool(opt.state & QStyle.State_Selected)
        if selected:
            # Keep selection colors stable across machines instead of relying on
            # the OS/Qt highlighted text palette, which can become unreadable.
            opt.palette.setColor(QPalette.Highlight, QColor(58, 68, 82))
            opt.palette.setColor(QPalette.HighlightedText, QColor(245, 248, 252))

        # Draw the standard item background/selection without the text
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style:
            style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget) if style else opt.rect
        ip_name = index.data(ROLE_IN_PROGRESS) or ""
        has_note = bool(index.data(ROLE_NOTE)) or bool(index.data(ROLE_ACTION_NOTE))
        is_new = bool(index.data(ROLE_IS_NEW))
        is_moved = bool(index.data(ROLE_IS_MOVED))
        is_updated = bool(index.data(ROLE_IS_UPDATED))
        has_pdf = bool(index.data(ROLE_HAS_PDF))
        has_late_pdf = bool(index.data(ROLE_HAS_LATE_PDF))
        has_excel = bool(index.data(ROLE_HAS_EXCEL))
        has_orders_form = bool(index.data(ROLE_HAS_ORDERS_FORM))
        has_qr_roster = bool(index.data(ROLE_HAS_QR_ROSTER))
        has_qr_orders = bool(index.data(ROLE_HAS_QR_ORDERS))
        show_asset_markers = bool(getattr(opt.widget, "show_asset_markers", False))
        if not show_asset_markers:
            has_pdf = False
            has_late_pdf = False
            has_excel = False
            has_orders_form = False
            has_qr_roster = False
            has_qr_orders = False

        left_offset = 0
        if is_moved:
            painter.save()
            marker_color = QColor(46, 204, 113)
            marker_rect = option.rect.adjusted(0, 1, 0, -1)
            marker_rect.setWidth(4)
            painter.fillRect(marker_rect, marker_color)
            painter.restore()
            left_offset += 4

        if is_new:
            painter.save()
            marker_color = QColor(0, 184, 255)
            marker_rect = option.rect.adjusted(left_offset, 1, 0, -1)
            marker_rect.setWidth(4)
            painter.fillRect(marker_rect, marker_color)
            painter.restore()
            left_offset += 4

        if is_updated:
            painter.save()
            marker_color = QColor(255, 152, 0)
            marker_rect = option.rect.adjusted(left_offset, 1, 0, -1)
            marker_rect.setWidth(4)
            painter.fillRect(marker_rect, marker_color)
            painter.restore()
            left_offset += 4

        painter.save()
        text_rect = text_rect.adjusted(left_offset + 2, 0, 0, 0)
        painter.setClipRect(text_rect)

        fm = painter.fontMetrics()
        baseline = text_rect.y() + (text_rect.height() + fm.ascent() - fm.descent()) // 2

        x = text_rect.x()
        if show_asset_markers:
            asset_gap = " "
            asset_mark = "▮"
            inactive_mark = "▯"
            asset_specs = [
                (has_orders_form, QColor(245, 194, 107), QColor(255, 216, 150)),
                (has_pdf, QColor(99, 179, 255), QColor(150, 210, 255)),
                (has_late_pdf, QColor(255, 99, 99), QColor(255, 150, 150)),
                (has_excel, QColor(111, 208, 140), QColor(156, 232, 174)),
                (has_qr_roster, QColor(182, 145, 255), QColor(207, 181, 255)),
                (has_qr_orders, QColor(255, 143, 205), QColor(255, 178, 222)),
            ]

            for idx, (is_on, on_color, selected_on_color) in enumerate(asset_specs):
                mark = asset_mark if is_on else inactive_mark
                if is_on:
                    pen_color = selected_on_color if selected else on_color
                else:
                    pen_color = QColor(124, 132, 145)
                painter.setPen(pen_color)
                painter.drawText(x, baseline, mark)
                x += fm.horizontalAdvance(mark)
                if idx < len(asset_specs) - 1:
                    painter.setPen(QColor(124, 132, 145))
                    painter.drawText(x, baseline, asset_gap)
                    x += fm.horizontalAdvance(asset_gap)
            painter.setPen(QColor(124, 132, 145))
            painter.drawText(x, baseline, asset_gap)
            x += fm.horizontalAdvance(asset_gap)

        if ip_name:
            prefix = f"{ip_name} "
            prefix_width = fm.horizontalAdvance(prefix)
            if prefix_width >= text_rect.width():
                prefix = fm.elidedText(prefix, Qt.ElideRight, text_rect.width())
                painter.setPen(QColor(255, 255, 0))
                painter.drawText(x, baseline, prefix)
                painter.restore()
                return

            painter.setPen(QColor(255, 255, 0))
            painter.drawText(x, baseline, prefix)
            x += prefix_width

        available = max(0, text_rect.width() - (x - text_rect.x()))
        if available > 0:
            elided = fm.elidedText(text, Qt.ElideRight, available)
            if has_note:
                painter.setPen(QColor(102, 255, 153) if selected else QColor(0, 255, 0))
            else:
                painter.setPen(QColor(245, 248, 252) if selected else opt.palette.color(QPalette.Text))
            painter.drawText(x, baseline, elided)

        painter.restore()


class NoteEditorDialog(QDialog):
    _RICH_PREFIX = "__DAMY_RICH_NOTE_HTML__:"
    _DEFAULT_READABLE_FONT_PT = 22
    _MIN_READABLE_FONT_PT = 18
    _VIEW_ZOOM_STEPS = 3

    def __init__(self, parent, school_name: str, note_text: str, *, note_label: str = "Note"):
        super().__init__(parent)
        self._note_label = str(note_label or "Note").strip() or "Note"
        self.setWindowTitle(f"Edit {self._note_label}")
        self.setModal(True)
        default_w = 980
        default_h = 820
        win = parent.window() if parent is not None else None
        if win is not None:
            try:
                default_w = max(default_w, int(win.width() * 0.48))
                default_h = max(default_h, int(win.height() * 0.78))
            except Exception:
                pass
        self.resize(default_w, default_h)
        self.setMinimumSize(920, 760)
        self._font_size = self._DEFAULT_READABLE_FONT_PT
        self._suppress_format_sync = False

        self.setStyleSheet(
            "QDialog { background-color: #2e2e2e; color: white; }"
            "QLabel { color: white; font-size: 18px; }"
            "QPushButton { background-color: #3a3a3a; color: white; border: 1px solid #555; padding: 6px 10px; }"
            "QPushButton:checked { background-color: #4b5e7a; border-color: #6d85a5; }"
            "QTextEdit { background-color: #202020; color: white; border: 1px solid #555; padding: 8px; font-size: 22px; }"
        )

        layout = QVBoxLayout(self)

        header = QLabel(f"School: {school_name}\n\n{self._note_label}:")
        header.setWordWrap(True)
        layout.addWidget(header)

        tools = QHBoxLayout()
        self.btn_smaller = QPushButton("A-")
        self.btn_larger = QPushButton("A+")
        self.btn_bold = QPushButton("Bold")
        self.btn_bold.setCheckable(True)
        tools.addWidget(self.btn_smaller)
        tools.addWidget(self.btn_larger)
        tools.addWidget(self.btn_bold)
        tools.addStretch(1)
        layout.addLayout(tools)

        self.editor = QTextEdit(self)
        self.editor.setAcceptRichText(True)
        self.editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.editor.setPlaceholderText(f"Type {self._note_label.lower()} here...")
        try:
            self.editor.zoomIn(int(self._VIEW_ZOOM_STEPS))
        except Exception:
            pass
        self._set_initial_note(note_text or "")
        layout.addWidget(self.editor, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_save = QPushButton("Save")
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_save)
        layout.addLayout(footer)

        self.btn_smaller.clicked.connect(self._decrease_font)
        self.btn_larger.clicked.connect(self._increase_font)
        self.btn_bold.clicked.connect(self._toggle_bold)
        btn_cancel.clicked.connect(self._handle_cancel_clicked)
        btn_save.clicked.connect(self._handle_save_clicked)
        self.editor.cursorPositionChanged.connect(self._sync_toolbar_state_from_cursor)

        self._sync_toolbar_state_from_cursor()

    @classmethod
    def _decode_note_for_editor(cls, raw_note: str) -> tuple[str, bool]:
        s = str(raw_note or "")
        if s.startswith(cls._RICH_PREFIX):
            return s[len(cls._RICH_PREFIX):], True
        sample = s.lstrip().lower()
        if sample.startswith("<!doctype") or sample.startswith("<html") or sample.startswith("<p"):
            return s, True
        return s, False

    def _set_initial_note(self, note_text: str) -> None:
        payload, is_html = self._decode_note_for_editor(note_text)
        if is_html:
            self.editor.setHtml(payload)
        else:
            self.editor.setPlainText(payload)
            default_font = self.editor.currentFont()
            default_font.setPointSize(int(self._DEFAULT_READABLE_FONT_PT))
            self.editor.setCurrentFont(default_font)
        self.editor.document().setModified(False)

    def _sync_toolbar_state_from_cursor(self) -> None:
        if self._suppress_format_sync:
            return
        cursor = self.editor.textCursor()
        fmt = cursor.charFormat()
        pt = fmt.fontPointSize()
        if pt and pt > 0:
            self._font_size = int(round(pt))
        else:
            fallback = self.editor.currentFont().pointSize()
            if fallback and fallback > 0:
                self._font_size = int(fallback)
        self.btn_bold.setChecked(bool(fmt.fontWeight() >= int(QFont.Weight.Bold)))

    def _apply_format_to_selection_or_cursor(self, fmt: QTextCharFormat) -> None:
        self._suppress_format_sync = True
        try:
            cursor = self.editor.textCursor()
            if not cursor.hasSelection():
                # User requested: if nothing is selected, treat it as "apply to all".
                cursor.select(cursor.SelectionType.Document)
            cursor.mergeCharFormat(fmt)
            self.editor.setTextCursor(cursor)
            self.editor.mergeCurrentCharFormat(fmt)
            self.editor.setFocus()
        finally:
            self._suppress_format_sync = False
            self._sync_toolbar_state_from_cursor()

    def _decrease_font(self) -> None:
        self._change_font_size(-1)

    def _increase_font(self) -> None:
        self._change_font_size(+1)

    def _change_font_size(self, delta: int) -> None:
        self._font_size = max(self._MIN_READABLE_FONT_PT, min(36, int(self._font_size + int(delta))))
        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(self._font_size))
        self._apply_format_to_selection_or_cursor(fmt)

    def _toggle_bold(self) -> None:
        fmt = QTextCharFormat()
        is_bold = bool(self.btn_bold.isChecked())
        fmt.setFontWeight(int(QFont.Weight.Bold if is_bold else QFont.Weight.Normal))
        self._apply_format_to_selection_or_cursor(fmt)

    def note_text(self) -> str:
        return self.editor.toPlainText()

    def note_payload(self) -> str:
        return f"{self._RICH_PREFIX}{self.editor.toHtml()}"

    def _is_dirty(self) -> bool:
        return bool(self.editor.document().isModified())

    def _confirm_discard_or_save(self) -> str:
        if not self._is_dirty():
            return "discard"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Unsaved Note Changes")
        box.setText("This note has unsaved changes.")
        box.setInformativeText("Do you want to save before closing?")
        save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return "save"
        if clicked is discard_btn:
            return "discard"
        if clicked is cancel_btn:
            return "cancel"
        return "cancel"

    def _handle_save_clicked(self) -> None:
        self.editor.document().setModified(False)
        self.accept()

    def _handle_cancel_clicked(self) -> None:
        decision = self._confirm_discard_or_save()
        if decision == "save":
            self._handle_save_clicked()
            return
        if decision == "discard":
            self.editor.document().setModified(False)
            self.reject()

    def reject(self) -> None:
        if not self.isVisible():
            super().reject()
            return
        decision = self._confirm_discard_or_save()
        if decision == "save":
            self._handle_save_clicked()
            return
        if decision == "discard":
            self.editor.document().setModified(False)
            super().reject()


class _RichNotePane(QWidget):
    def __init__(self, parent, *, title: str, initial_note: str):
        super().__init__(parent)
        self._title = str(title or "Note")
        self._font_size = NoteEditorDialog._DEFAULT_READABLE_FONT_PT
        self._suppress_sync = False
        self._build_ui(initial_note or "")

    def _build_ui(self, initial_note: str) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(self._title)
        title.setWordWrap(True)
        layout.addWidget(title)

        tools = QHBoxLayout()
        self.btn_smaller = QPushButton("A-")
        self.btn_larger = QPushButton("A+")
        self.btn_bold = QPushButton("Bold")
        self.btn_bold.setCheckable(True)
        tools.addWidget(self.btn_smaller)
        tools.addWidget(self.btn_larger)
        tools.addWidget(self.btn_bold)
        tools.addStretch(1)
        layout.addLayout(tools)

        self.editor = QTextEdit(self)
        self.editor.setAcceptRichText(True)
        self.editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.editor.setPlaceholderText(f"Type {self._title.lower()} here...")
        try:
            self.editor.zoomIn(int(NoteEditorDialog._VIEW_ZOOM_STEPS))
        except Exception:
            pass
        self._set_initial_note(initial_note)
        layout.addWidget(self.editor, 1)

        self.btn_smaller.clicked.connect(self._decrease_font)
        self.btn_larger.clicked.connect(self._increase_font)
        self.btn_bold.clicked.connect(self._toggle_bold)
        self.editor.cursorPositionChanged.connect(self._sync_toolbar_state_from_cursor)
        self._sync_toolbar_state_from_cursor()

    def _set_initial_note(self, note_text: str) -> None:
        payload, is_html = NoteEditorDialog._decode_note_for_editor(note_text)
        if is_html:
            self.editor.setHtml(payload)
        else:
            self.editor.setPlainText(payload)
            font = self.editor.currentFont()
            font.setPointSize(int(NoteEditorDialog._DEFAULT_READABLE_FONT_PT))
            self.editor.setCurrentFont(font)
        self.editor.document().setModified(False)

    def _sync_toolbar_state_from_cursor(self) -> None:
        if self._suppress_sync:
            return
        cursor = self.editor.textCursor()
        fmt = cursor.charFormat()
        pt = fmt.fontPointSize()
        if pt and pt > 0:
            self._font_size = int(round(pt))
        else:
            fallback = self.editor.currentFont().pointSize()
            if fallback and fallback > 0:
                self._font_size = int(fallback)
        self.btn_bold.setChecked(bool(fmt.fontWeight() >= int(QFont.Weight.Bold)))

    def _apply_format_to_selection_or_cursor(self, fmt: QTextCharFormat) -> None:
        self._suppress_sync = True
        try:
            cursor = self.editor.textCursor()
            if not cursor.hasSelection():
                cursor.select(cursor.SelectionType.Document)
            cursor.mergeCharFormat(fmt)
            self.editor.setTextCursor(cursor)
            self.editor.mergeCurrentCharFormat(fmt)
            self.editor.setFocus()
        finally:
            self._suppress_sync = False
            self._sync_toolbar_state_from_cursor()

    def _decrease_font(self) -> None:
        self._change_font_size(-1)

    def _increase_font(self) -> None:
        self._change_font_size(+1)

    def _change_font_size(self, delta: int) -> None:
        self._font_size = max(
            NoteEditorDialog._MIN_READABLE_FONT_PT,
            min(36, int(self._font_size + int(delta))),
        )
        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(self._font_size))
        self._apply_format_to_selection_or_cursor(fmt)

    def _toggle_bold(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(int(QFont.Weight.Bold if self.btn_bold.isChecked() else QFont.Weight.Normal))
        self._apply_format_to_selection_or_cursor(fmt)

    def note_text(self) -> str:
        return self.editor.toPlainText()

    def note_payload(self) -> str:
        return f"{NoteEditorDialog._RICH_PREFIX}{self.editor.toHtml()}"

    def is_dirty(self) -> bool:
        return bool(self.editor.document().isModified())

    def mark_clean(self) -> None:
        self.editor.document().setModified(False)


class NotesEditorDialog(QDialog):
    def __init__(self, parent, *, school_name: str, source_note_text: str, action_note_text: str):
        super().__init__(parent)
        self.setWindowTitle("Edit Notes")
        self.setModal(True)
        self.resize(1420, 840)
        self.setMinimumSize(1220, 740)
        self.setStyleSheet(
            "QDialog { background-color: #2e2e2e; color: white; }"
            "QLabel { color: white; font-size: 18px; font-weight: 600; }"
            "QPushButton { background-color: #3a3a3a; color: white; border: 1px solid #555; padding: 6px 10px; }"
            "QPushButton:checked { background-color: #4b5e7a; border-color: #6d85a5; }"
            "QTextEdit { background-color: #202020; color: white; border: 1px solid #555; padding: 8px; font-size: 22px; }"
            "QLineEdit { background-color: #202020; color: white; border: 1px solid #555; padding: 8px; font-size: 18px; }"
        )

        layout = QVBoxLayout(self)
        header = QLabel(f"School: {school_name}")
        header.setWordWrap(True)
        layout.addWidget(header)

        panes = QHBoxLayout()
        self.source_pane = _RichNotePane(self, title="Source Note", initial_note=source_note_text)
        self.action_pane = _RichNotePane(self, title="Action Note", initial_note=action_note_text)
        panes.addWidget(self.source_pane, 1)
        panes.addWidget(self.action_pane, 1)
        layout.addLayout(panes, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_save = QPushButton("Save")
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_save)
        layout.addLayout(footer)
        btn_cancel.clicked.connect(self._handle_cancel_clicked)
        btn_save.clicked.connect(self._handle_save_clicked)

    def _is_dirty(self) -> bool:
        return bool(self.source_pane.is_dirty() or self.action_pane.is_dirty())

    def _confirm_discard_or_save(self) -> str:
        if not self._is_dirty():
            return "discard"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Unsaved Note Changes")
        box.setText("There are unsaved changes in Notes.")
        box.setInformativeText("Do you want to save before closing?")
        save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return "save"
        if clicked is discard_btn:
            return "discard"
        if clicked is cancel_btn:
            return "cancel"
        return "cancel"

    def _handle_save_clicked(self) -> None:
        self.source_pane.mark_clean()
        self.action_pane.mark_clean()
        self.accept()

    def _handle_cancel_clicked(self) -> None:
        decision = self._confirm_discard_or_save()
        if decision == "save":
            self._handle_save_clicked()
            return
        if decision == "discard":
            self.source_pane.mark_clean()
            self.action_pane.mark_clean()
            self.reject()

    def reject(self) -> None:
        if not self.isVisible():
            super().reject()
            return
        decision = self._confirm_discard_or_save()
        if decision == "save":
            self._handle_save_clicked()
            return
        if decision == "discard":
            self.source_pane.mark_clean()
            self.action_pane.mark_clean()
            super().reject()

    def source_note_text(self) -> str:
        return self.source_pane.note_text()

    def source_note_payload(self) -> str:
        return self.source_pane.note_payload()

    def action_note_text(self) -> str:
        return self.action_pane.note_text()

    def action_note_payload(self) -> str:
        return self.action_pane.note_payload()


class ContactEditorDialog(QDialog):
    def __init__(
        self,
        parent,
        *,
        school_name: str,
        contact_name: str = "",
        contact_email: str = "",
        contact_phone: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle("Edit Contact")
        self.setModal(True)
        self.resize(780, 420)
        self.setMinimumSize(680, 360)
        self.setStyleSheet(
            "QDialog { background-color: #2e2e2e; color: white; }"
            "QLabel { color: white; font-size: 18px; }"
            "QPushButton { background-color: #3a3a3a; color: white; border: 1px solid #555; padding: 6px 10px; }"
            "QLineEdit { background-color: #202020; color: white; border: 1px solid #555; padding: 8px; font-size: 20px; }"
        )

        layout = QVBoxLayout(self)
        header = QLabel(f"School: {school_name}")
        header.setWordWrap(True)
        layout.addWidget(header)

        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("Contact Name")
        self.name_edit.setText(str(contact_name or "").strip())
        layout.addWidget(QLabel("Name:"))
        layout.addWidget(self.name_edit)

        self.email_edit = QLineEdit(self)
        self.email_edit.setPlaceholderText("Email (first email only)")
        self.email_edit.setText(str(contact_email or "").strip())
        layout.addWidget(QLabel("Email:"))
        layout.addWidget(self.email_edit)

        self.phone_edit = QLineEdit(self)
        self.phone_edit.setPlaceholderText("Phone")
        self.phone_edit.setText(str(contact_phone or "").strip())
        layout.addWidget(QLabel("Phone:"))
        layout.addWidget(self.phone_edit)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_save = QPushButton("Save")
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_save)
        layout.addLayout(footer)
        btn_cancel.clicked.connect(self.reject)
        btn_save.clicked.connect(self.accept)

    def contact_values(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        name_value = (self.name_edit.text() or "").strip() or None
        email_raw = (self.email_edit.text() or "").strip()
        phone_value = (self.phone_edit.text() or "").strip() or None
        email_match = re.search(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", email_raw, flags=re.IGNORECASE)
        email_value = (email_match.group(0).strip() if email_match else "") or None
        return name_value, email_value, phone_value


class DragListWidget(QListWidget):
    # Emits: item_id, old_stage, new_stage, disk_name
    folderDropped = Signal(int, int, int, str)

    itemRemovedFromEdit = Signal(int)     # emits item_id
    uiReloadRequested = Signal()

    def __init__(self, base_dir: str, day_name: str, requires_flags_to_leave: bool = False):
        super().__init__()
        self.base_dir = base_dir
        self.day_name = day_name
        self.requires_flags_to_leave = requires_flags_to_leave
        self.source_base_dir = (os.getenv("DAMY_SOURCE_BASE_DIR") or "").strip()

        # DB wiring (set by main_window via set_db / set_stage)
        self.db = None
        self.stage: Optional[int] = None

        # For logging
        self.current_user: str = os.environ.get("USERNAME") or os.environ.get("USER") or "UNKNOWN"

        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        # Asset markers are intentionally disabled in list rows.
        self.show_asset_markers = False
        # Use pixel-based scrolling so large inline widgets (like Folder Assets)
        # enter/exit viewport boundaries progressively instead of row-snapping.
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(14)
        self.setItemDelegate(InProgressPrefixDelegate(self))

        self.itemDoubleClicked.connect(self.open_folder)
        self.itemClicked.connect(self.refresh_item_from_db)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    # ---- Wiring from main_window.py ----
    def set_db(self, db):
        self.db = db

    def set_stage(self, stage: int):
        self.stage = int(stage)
        self.show_asset_markers = bool(self.stage == 1)
        for idx in range(self.count()):
            item = self.item(idx)
            if item is not None:
                self._refresh_item_tooltip(item)
        self.viewport().update()

    def set_current_user(self, name: str):
        if name:
            self.current_user = name

    # ---- Add items (called by main_window.py) ----

    def add_entry(self, disk_name: str, display_text: str):
        item = QListWidgetItem(display_text)
        item.setData(ROLE_DISK_NAME, disk_name)
        self.addItem(item)

    def _refresh_item_tooltip(self, item: QListWidgetItem) -> None:
        lines: list[str] = []
        in_progress = (item.data(ROLE_IN_PROGRESS) or "").strip()
        source_note_text = (item.data(ROLE_NOTE) or "").strip()
        action_note_text = (item.data(ROLE_ACTION_NOTE) or "").strip()
        has_contact = bool(item.data(ROLE_HAS_CONTACT))
        show_assets = True
        try:
            if self.stage is not None and int(self.stage) != 1:
                show_assets = False
        except Exception:
            show_assets = True
        has_pdf = bool(item.data(ROLE_HAS_PDF))
        has_late_pdf = bool(item.data(ROLE_HAS_LATE_PDF))
        has_excel = bool(item.data(ROLE_HAS_EXCEL))
        has_orders_form = bool(item.data(ROLE_HAS_ORDERS_FORM))
        has_qr_roster = bool(item.data(ROLE_HAS_QR_ROSTER))
        has_qr_orders = bool(item.data(ROLE_HAS_QR_ORDERS))

        if in_progress:
            lines.append(f"In progress: {in_progress}")
        if source_note_text:
            lines.append("Source note saved")
        if action_note_text:
            lines.append("Action note saved")
        if has_contact:
            lines.append("Contact saved")
        if show_assets:
            asset_parts = []
            if has_orders_form:
                asset_parts.append("Make Orders Form linked")
            if has_pdf:
                asset_parts.append("PDF linked")
            if has_late_pdf:
                asset_parts.append("Late PDF linked")
            if has_excel:
                asset_parts.append("Excel linked")
            if has_qr_roster:
                asset_parts.append("QR Roster linked")
            if has_qr_orders:
                asset_parts.append("QR Orders linked")
            if asset_parts:
                lines.append("Assets: " + ", ".join(asset_parts))

        item.setToolTip("\n".join(lines))

    def apply_in_progress_style(self, item: QListWidgetItem, name: Optional[str]) -> None:
        if name:
            item.setData(ROLE_IN_PROGRESS, name)
        else:
            item.setData(ROLE_IN_PROGRESS, None)
        self._refresh_item_tooltip(item)

    def apply_note_style(self, item: QListWidgetItem, note: Optional[str]) -> None:
        if note:
            item.setData(ROLE_NOTE, note)
        else:
            item.setData(ROLE_NOTE, None)
        self._refresh_item_tooltip(item)

    def apply_action_note_style(self, item: QListWidgetItem, action_note: Optional[str]) -> None:
        if action_note:
            item.setData(ROLE_ACTION_NOTE, action_note)
        else:
            item.setData(ROLE_ACTION_NOTE, None)
        self._refresh_item_tooltip(item)

    def apply_contact_style(self, item: QListWidgetItem, has_contact: bool) -> None:
        item.setData(ROLE_HAS_CONTACT, bool(has_contact))
        self._refresh_item_tooltip(item)

    def apply_note_color_style(self, item: QListWidgetItem, color_hex: Optional[str]) -> None:
        if color_hex:
            color = QColor(color_hex)
            if color.isValid():
                item.setData(ROLE_NOTE_COLOR, color_hex)
                item.setBackground(QBrush(color))
                return

        item.setData(ROLE_NOTE_COLOR, None)
        item.setBackground(QBrush())

    def apply_new_import_style(self, item: QListWidgetItem, is_new: bool) -> None:
        item.setData(ROLE_IS_NEW, bool(is_new))
        tip = item.toolTip() or ""
        if is_new:
            if "New from calendar import" not in tip:
                item.setToolTip("New from calendar import")

    def apply_moved_style(self, item: QListWidgetItem, is_moved: bool) -> None:
        item.setData(ROLE_IS_MOVED, bool(is_moved))

    def apply_updated_style(self, item: QListWidgetItem, is_updated: bool) -> None:
        item.setData(ROLE_IS_UPDATED, bool(is_updated))

    def apply_asset_presence_style(
        self,
        item: QListWidgetItem,
        *,
        has_pdf: bool,
        has_excel: bool,
        has_late_pdf: bool = False,
        has_orders_form: bool = False,
        has_qr_roster: bool = False,
        has_qr_orders: bool = False,
    ) -> None:
        item.setData(ROLE_HAS_PDF, bool(has_pdf))
        item.setData(ROLE_HAS_LATE_PDF, bool(has_late_pdf))
        item.setData(ROLE_HAS_EXCEL, bool(has_excel))
        item.setData(ROLE_HAS_ORDERS_FORM, bool(has_orders_form))
        item.setData(ROLE_HAS_QR_ROSTER, bool(has_qr_roster))
        item.setData(ROLE_HAS_QR_ORDERS, bool(has_qr_orders))
        self._refresh_item_tooltip(item)

    def _asset_path_exists(self, raw_path: str) -> bool:
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            return False
        try:
            path = Path(cleaned).expanduser()
            if not path.is_absolute():
                path = (Path(self.base_dir) / path).resolve()
            return path.exists()
        except Exception:
            return False

    def _extract_school_name(self, display_name: str) -> str:
        s = (display_name or "").strip()
        s = re.sub(r"^\d{6}\s+", "", s)
        s = re.sub(r"\s+P\d{8,}\b.*$", "", s).strip()
        return s or (display_name or "").strip()

    def _extract_pid_value(self, *values: str) -> str | None:
        for value in values:
            text = (value or "").strip().upper()
            if not text:
                continue
            match = re.search(r"\bP\d{7,}\b", text)
            if match:
                return match.group(0)
        return None

    def refresh_item_from_db(self, item: QListWidgetItem) -> None:
        if self.db is None or item is None:
            return

        item_id = item.data(ROLE_DB_ID)
        if item_id is None:
            return

        try:
            db_item = self.db.get_item_by_id(int(item_id))
        except Exception:
            # Keep UI responsive; failures can happen if DB is temporarily unavailable.
            return

        if not db_item:
            return

        item.setText(db_item.display_name)
        item.setData(ROLE_DISK_NAME, db_item.disk_name)
        self.apply_in_progress_style(item, db_item.in_progress_by)
        self.apply_note_style(item, db_item.note)
        self.apply_action_note_style(item, getattr(db_item, "action_note", None))
        has_contact = bool(
            str(getattr(db_item, "contact_name", "") or "").strip()
            or str(getattr(db_item, "contact_email", "") or "").strip()
            or str(getattr(db_item, "contact_phone", "") or "").strip()
        )
        self.apply_contact_style(item, has_contact)
        self.apply_note_color_style(item, db_item.note_color)
        show_assets = bool(self.show_asset_markers)
        self.apply_asset_presence_style(
            item,
            has_pdf=(
                self._asset_path_exists(db_item.pdf_path or "")
                or self._asset_path_exists(getattr(db_item, "pdf_path_2", "") or "")
                or self._asset_path_exists(getattr(db_item, "pdf_path_3", "") or "")
                or self._asset_path_exists(getattr(db_item, "pdf_path_4", "") or "")
            ) if show_assets else False,
            has_late_pdf=self._asset_path_exists(getattr(db_item, "late_pdf_path", "") or "") if show_assets else False,
            has_excel=self._asset_path_exists(db_item.excel_path or "") if show_assets else False,
            has_orders_form=(
                self._asset_path_exists(db_item.orders_form_path or "")
                or self._asset_path_exists(getattr(db_item, "orders_form_path_2", "") or "")
                or self._asset_path_exists(getattr(db_item, "orders_form_path_3", "") or "")
                or self._asset_path_exists(getattr(db_item, "orders_form_path_4", "") or "")
            ) if show_assets else False,
            has_qr_roster=self._asset_path_exists(db_item.qr_roster_path or "") if show_assets else False,
            has_qr_orders=self._asset_path_exists(db_item.qr_orders_path or "") if show_assets else False,
        )

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.source() is not self:
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragEnterEvent):
        if event.source() is not self:
            event.acceptProposedAction()

    def startDrag(self, supportedActions):
        super().startDrag(Qt.MoveAction)

    # ---- DB-backed drop ----
    def dropEvent(self, event: QDropEvent):
        source: DragListWidget = event.source()
        if not source or source is self:
            return

        if self.db is None or self.stage is None:
            show_warning_unified(self, "DB Not Wired", "This widget has no DB/stage set.")
            return

        selected_items = [it for it in source.selectedItems() if it is not None]
        if not selected_items:
            current = source.currentItem()
            if current is not None:
                selected_items = [current]
        if not selected_items:
            return

        old_stage = source.stage if source.stage is not None else -1
        new_stage = self.stage

        move_rows: list[tuple[QListWidgetItem, int, str]] = []
        for item in selected_items:
            item_id = item.data(ROLE_DB_ID)
            disk_name = item.data(ROLE_DISK_NAME)
            if item_id is None or not disk_name:
                continue
            move_rows.append((item, int(item_id), str(disk_name)))

        if not move_rows:
            show_warning_unified(self, "Missing Data", "Selected rows are missing DB id or disk_name.")
            return

        # If moving OUT of Edit, require I and G flags for every selected row.
        if source.requires_flags_to_leave and not self.requires_flags_to_leave:
            blocked_names = []
            for _, item_id, disk_name in move_rows:
                try:
                    db_item = self.db.get_item_by_id(item_id)
                except Exception as e:
                    show_warning_unified(self, "DB Error", f"Could not read flags for item {item_id}.\n\n{e}")
                    return

                if not db_item:
                    show_warning_unified(self, "Missing DB Row", f"Item {item_id} not found in DB.")
                    return

                if not (db_item.flag_i and db_item.flag_g):
                    blocked_names.append(disk_name)

            if blocked_names:
                preview = "\n".join(blocked_names[:10])
                if len(blocked_names) > 10:
                    preview += f"\n...and {len(blocked_names) - 10} more"
                show_warning_unified(
                    self,
                    "Move Blocked",
                    "You must check both I and G before moving these folders out of "
                    f"'{source.day_name}':\n\n{preview}",
                )
                return

        moved: list[tuple[QListWidgetItem, int, str]] = []
        errors: list[str] = []
        for item, item_id, disk_name in move_rows:
            try:
                # UI drag/drop updates DB stage only. Disk folders are unchanged.
                self.db.update_stage(item_id=item_id, new_stage=new_stage)
                moved.append((item, item_id, disk_name))
            except Exception as e:
                errors.append(f"{disk_name}: {e}")

        if not moved and errors:
            preview = "\n".join(errors[:8])
            show_warning_unified(self, "DB Update Failed", f"Could not move selected rows.\n\n{preview}")
            return

        for item, item_id, _ in sorted(moved, key=lambda x: source.row(x[0]), reverse=True):
            source.takeItem(source.row(item))
            if source.requires_flags_to_leave and not self.requires_flags_to_leave:
                self.itemRemovedFromEdit.emit(item_id)

        event.acceptProposedAction()

        for _, item_id, disk_name in moved:
            self.folderDropped.emit(item_id, int(old_stage), int(new_stage), str(disk_name))
        self.uiReloadRequested.emit()

        if errors:
            preview = "\n".join(errors[:8])
            more = len(errors) - min(8, len(errors))
            if more > 0:
                preview += f"\n...and {more} more"
            show_warning_unified(self, "Partial Move Completed", f"Some rows failed to move:\n\n{preview}")

    # ---- Context menu: In Progress stored in DB (and logged) ----
    def show_context_menu(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return

        if self.db is None:
            show_warning_unified(self, "DB Not Wired", "This widget has no DB set yet.")
            return

        item = self.item(index.row())
        self.refresh_item_from_db(item)
        item_id = item.data(ROLE_DB_ID)
        disk_name = item.data(ROLE_DISK_NAME) or item.text()

        if item_id is None:
            show_warning_unified(self, "Missing Item ID", "This row has no DB id.")
            return

        item_id = int(item_id)

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #2e2e2e; color: white; border: 1px solid #444; }
            QMenu::item { padding: 6px 20px; background-color: transparent; }
            QMenu::item:selected { background-color: #444; }
        """)

        action_ip = menu.addAction("Set In Progress...")
        action_clear = menu.addAction("Clear In Progress")
        action_copy_pid = menu.addAction("Copy PID")
        action_edit_notes = menu.addAction("Edit Notes...")
        action_edit_contact = menu.addAction("Edit Contact...")
        action_set_note_color = menu.addAction("Set Note Background Color...")
        action_clear_note_color = menu.addAction("Clear Note Background Color")

        chosen = menu.exec(self.viewport().mapToGlobal(pos))

        if chosen == action_ip:
            dialog = QInputDialog(self)
            dialog.setWindowTitle("In Progress")
            dialog.setLabelText("Name of person working on it:")
            dialog.setStyleSheet("""
                QInputDialog { background-color: #2e2e2e; color: white; }
                QLineEdit { background-color: #3a3a3a; color: white; border: 1px solid #555; }
                QLabel { color: white; }
            """)
            ok = dialog.exec()
            name = dialog.textValue().strip() if ok else None
            if not ok or not name:
                return

            try:
                self.db.set_in_progress(item_id, name)
            except Exception as e:
                show_warning_unified(self, "DB Update Failed", f"Could not set in-progress.\n\n{e}")
                return

            # Log
            write_event(
                AuditEvent(
                    action="INPROGRESS",
                    item_id=item_id,
                    disk_name=disk_name,
                    value=name,
                ),
                base_dir=self.base_dir,
            )


            # Visual highlight only
            self.apply_in_progress_style(item, name)

        elif chosen == action_clear:
            try:
                self.db.set_in_progress(item_id, None)
            except Exception as e:
                show_warning_unified(self, "DB Update Failed", f"Could not clear in-progress.\n\n{e}")
                return

            # Log
            write_event(
                AuditEvent(
                    action="CLEARIP",
                    item_id=item_id,
                    disk_name=disk_name,
                ),
                base_dir=self.base_dir,
            )


            self.apply_in_progress_style(item, None)
        elif chosen == action_copy_pid:
            try:
                db_item = self.db.get_item_by_id(item_id)
            except Exception as e:
                show_warning_unified(self, "DB Error", f"Could not read item {item_id}.\n\n{e}")
                return

            pid_value = self._extract_pid_value(
                (db_item.pid if db_item else None) or "",
                (db_item.disk_name if db_item else None) or "",
                (db_item.display_name if db_item else None) or "",
                str(disk_name),
                item.text(),
            )
            if not pid_value:
                show_information_unified(self, "Copy PID", f"No PID found for:\n{item.text()}")
                return

            clipboard = QApplication.clipboard()
            clipboard.setText(pid_value)
        elif chosen == action_edit_notes:
            try:
                db_item = self.db.get_item_by_id(item_id)
            except Exception as e:
                show_warning_unified(self, "DB Error", f"Could not read item {item_id}.\n\n{e}")
                return

            current_source = (db_item.note if db_item else None) or ""
            current_action = (getattr(db_item, "action_note", None) if db_item else None) or ""
            display_name = (db_item.display_name if db_item else None) or item.text()
            school_name = self._extract_school_name(display_name)

            dialog = NotesEditorDialog(
                self,
                school_name=school_name,
                source_note_text=current_source,
                action_note_text=current_action,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            source_plain = dialog.source_note_text().strip()
            action_plain = dialog.action_note_text().strip()
            new_source_note = dialog.source_note_payload().strip() if source_plain else None
            new_action_note = dialog.action_note_payload().strip() if action_plain else None

            try:
                self.db.set_note(item_id, new_source_note)
                self.db.set_action_note(item_id, new_action_note)
            except Exception as e:
                show_warning_unified(self, "DB Update Failed", f"Could not update notes.\n\n{e}")
                return
            self.apply_note_style(item, new_source_note)
            self.apply_action_note_style(item, new_action_note)
        elif chosen == action_edit_contact:
            try:
                db_item = self.db.get_item_by_id(item_id)
            except Exception as e:
                show_warning_unified(self, "DB Error", f"Could not read item {item_id}.\n\n{e}")
                return

            display_name = (db_item.display_name if db_item else None) or item.text()
            school_name = self._extract_school_name(display_name)
            current_name = (getattr(db_item, "contact_name", None) if db_item else None) or ""
            current_email = (getattr(db_item, "contact_email", None) if db_item else None) or ""
            current_phone = (getattr(db_item, "contact_phone", None) if db_item else None) or ""

            if not (str(current_name).strip() or str(current_email).strip() or str(current_phone).strip()):
                parsed_name, parsed_email, parsed_phone = parse_contact_fields_from_note((db_item.note if db_item else None) or "")
                current_name = current_name or (parsed_name or "")
                current_email = current_email or (parsed_email or "")
                current_phone = current_phone or (parsed_phone or "")

            dialog = ContactEditorDialog(
                self,
                school_name=school_name,
                contact_name=str(current_name or ""),
                contact_email=str(current_email or ""),
                contact_phone=str(current_phone or ""),
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            new_name, new_email, new_phone = dialog.contact_values()

            try:
                self.db.set_contact(
                    item_id,
                    contact_name=new_name,
                    contact_email=new_email,
                    contact_phone=new_phone,
                )
            except Exception as e:
                show_warning_unified(self, "DB Update Failed", f"Could not update contact.\n\n{e}")
                return
            self.apply_contact_style(item, bool(new_name or new_email or new_phone))
        elif chosen == action_set_note_color:
            color = QColorDialog.getColor(parent=self, title="Select Note Background Color")
            if not color.isValid():
                return

            color_hex = color.name(QColor.HexRgb)
            try:
                self.db.set_note_color(item_id, color_hex)
            except Exception as e:
                show_warning_unified(self, "DB Update Failed", f"Could not set note background color.\n\n{e}")
                return
            self.apply_note_color_style(item, color_hex)
        elif chosen == action_clear_note_color:
            try:
                self.db.set_note_color(item_id, None)
            except Exception as e:
                show_warning_unified(self, "DB Update Failed", f"Could not clear note background color.\n\n{e}")
                return
            self.apply_note_color_style(item, None)

    # ---- Open folder on disk (still uses disk_name) ----
    def open_folder(self, item: QListWidgetItem):
        disk_name = item.data(ROLE_DISK_NAME)
        if not disk_name:
            show_warning_unified(self, "Open Folder Failed", "Missing disk_name.")
            return

        path = os.path.join(self.base_dir, disk_name)
        if not os.path.isdir(path):
            cancel_path = os.path.join(self.base_dir, "cancel", disk_name)
            if os.path.isdir(cancel_path):
                path = cancel_path
        if not os.path.isdir(path) and self.source_base_dir:
            source_path = os.path.join(self.source_base_dir, disk_name)
            if os.path.isdir(source_path):
                path = source_path
            else:
                source_cancel_path = os.path.join(self.source_base_dir, "cancel", disk_name)
                if os.path.isdir(source_cancel_path):
                    path = source_cancel_path

        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", path])
        except Exception as e:
            show_warning_unified(self, "Open Folder Failed", f"Could not open:\n{path}\n{e}")
