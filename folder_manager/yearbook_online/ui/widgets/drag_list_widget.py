import json
import os
import re
import subprocess
import sys
from typing import Optional

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDrag, QDragEnterEvent, QDropEvent, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
)

DAY_NAME_ALIASES = {
    "2. Sort": ("2. Sort", "2. Photodeck Upload"),
    "3. Photodeck Upload/PDF Packets": (
        "3. Photodeck Upload/PDF Packets",
        "3. Photodeck Upload",
        "3. PDF Packets",
    ),
}

ROLE_DISK_NAME = Qt.UserRole
ROLE_DB_ID = Qt.UserRole + 1
ROLE_IN_PROGRESS = Qt.UserRole + 2
ROLE_IS_NEW = Qt.UserRole + 5
ROLE_IS_MOVED = Qt.UserRole + 6
ROLE_IS_UPDATED = Qt.UserRole + 7
MIME_WORKFLOW_ITEMS = "application/x-damy-workflow-items"


class _WorkflowDropProxy:
    def __init__(self, source, mime: QMimeData):
        self._source = source
        self._mime = mime
        self.accepted = False
        self.drop_action = None

    def source(self):
        return self._source

    def mimeData(self):
        return self._mime

    def setDropAction(self, action):
        self.drop_action = action

    def accept(self):
        self.accepted = True


class InProgressPrefixDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        selected = bool(opt.state & QStyle.State_Selected)
        if selected:
            opt.palette.setColor(QPalette.Highlight, QColor(58, 68, 82))
            opt.palette.setColor(QPalette.HighlightedText, QColor(245, 248, 252))

        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style:
            style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget) if style else opt.rect
        ip_name = index.data(ROLE_IN_PROGRESS) or ""
        is_new = bool(index.data(ROLE_IS_NEW))
        is_moved = bool(index.data(ROLE_IS_MOVED))
        is_updated = bool(index.data(ROLE_IS_UPDATED))

        left_offset = 0
        if is_moved:
            painter.save()
            marker_rect = option.rect.adjusted(0, 1, 0, -1)
            marker_rect.setWidth(4)
            painter.fillRect(marker_rect, QColor(46, 204, 113))
            painter.restore()
            left_offset += 4
        if is_new:
            painter.save()
            marker_rect = option.rect.adjusted(left_offset, 1, 0, -1)
            marker_rect.setWidth(4)
            painter.fillRect(marker_rect, QColor(0, 184, 255))
            painter.restore()
            left_offset += 4
        if is_updated:
            painter.save()
            marker_rect = option.rect.adjusted(left_offset, 1, 0, -1)
            marker_rect.setWidth(4)
            painter.fillRect(marker_rect, QColor(255, 152, 0))
            painter.restore()
            left_offset += 4

        painter.save()
        text_rect = text_rect.adjusted(left_offset + 2, 0, 0, 0)
        painter.setClipRect(text_rect)
        fm = painter.fontMetrics()
        baseline = text_rect.y() + (text_rect.height() + fm.ascent() - fm.descent()) // 2
        x = text_rect.x()

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
            painter.setPen(QColor(245, 248, 252) if selected else opt.palette.color(QPalette.Text))
            painter.drawText(x, baseline, elided)
        painter.restore()


def _extract_suffix_by_day_name(folder_name: str, day_name: str) -> str:
    prefixes = DAY_NAME_ALIASES.get(day_name, (day_name,))
    for prefix in prefixes:
        if prefix in folder_name:
            return folder_name.split(prefix, 1)[-1].strip()
    return folder_name.strip()


class DragListWidget(QListWidget):
    folderDropped = Signal()
    folderDroppedDetailed = Signal(int, int, int, str)
    itemRemovedFromEdit = Signal(int)
    uiReloadRequested = Signal()

    def __init__(self, base_dir: str, day_name: str):
        super().__init__()
        self.base_dir = base_dir
        self.day_name = day_name
        self.db = None
        self.workflow_domain: Optional[str] = None
        self.workflow_stage: Optional[int] = None
        self.source_base_dir: Optional[str] = (os.environ.get("DAMY_SOURCE_BASE_DIR") or "").strip() or None
        self._manual_drag_start_pos = None
        self._manual_drag_payload: Optional[dict] = None
        self._manual_drag_active = False

        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(14)
        self.setItemDelegate(InProgressPrefixDelegate(self))

        self.itemDoubleClicked.connect(self.open_folder)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def set_db(self, db) -> None:
        self.db = db

    def set_workflow_domain(self, domain: str) -> None:
        self.workflow_domain = (domain or "").strip().lower() or None

    def set_workflow_stage(self, stage: int) -> None:
        try:
            self.workflow_stage = int(stage)
        except Exception:
            self.workflow_stage = None

    def _domain_key(self) -> str:
        key = re.sub(r"[^a-z0-9_-]+", "_", str(self.workflow_domain or "").strip().lower())
        return key or "yearbook"

    def _runtime_workspace_root(self) -> str:
        return os.path.join(self.base_dir, "_workflow_runtime", self._domain_key())

    def _candidate_open_paths(self, disk_name: str) -> list[str]:
        name = str(disk_name or "").strip()
        if not name:
            return []
        candidates: list[str] = []
        stage = int(self.workflow_stage or 0)
        if stage == 7:
            candidates.append(os.path.join(self._runtime_workspace_root(), "send_to_edit", name))
        elif stage == 8:
            candidates.append(os.path.join(self._runtime_workspace_root(), "print", name))
        candidates.append(os.path.join(self.base_dir, name))
        candidates.append(os.path.join(self.base_dir, "cancel", name))
        if self.source_base_dir:
            candidates.append(os.path.join(self.source_base_dir, name))
            candidates.append(os.path.join(self.source_base_dir, "cancel", name))
        return candidates

    def add_entry(self, folder_name: str, display_text: str, item_id: Optional[int] = None, in_progress_by: Optional[str] = None) -> None:
        item = QListWidgetItem(display_text)
        item.setData(ROLE_DISK_NAME, folder_name)
        if item_id is not None:
            item.setData(ROLE_DB_ID, int(item_id))
        item.setData(ROLE_IN_PROGRESS, str(in_progress_by or "").strip() or None)
        self.addItem(item)

    def apply_in_progress_style(self, item: QListWidgetItem, name: Optional[str]) -> None:
        item.setData(ROLE_IN_PROGRESS, str(name or "").strip() or None)
        self.viewport().update()

    def apply_new_import_style(self, item: QListWidgetItem, is_new: bool) -> None:
        item.setData(ROLE_IS_NEW, bool(is_new))

    def apply_moved_style(self, item: QListWidgetItem, is_moved: bool) -> None:
        item.setData(ROLE_IS_MOVED, bool(is_moved))

    def apply_updated_style(self, item: QListWidgetItem, is_updated: bool) -> None:
        item.setData(ROLE_IS_UPDATED, bool(is_updated))

    def mousePressEvent(self, event):
        self.clearSelection()
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            pos = self._event_pos(event)
            if self.itemAt(pos) is not None:
                self._manual_drag_start_pos = pos
                self._manual_drag_payload = self._workflow_drag_payload(self._iter_source_items(self))
                self._manual_drag_active = False
            else:
                self._reset_manual_drag()

    def mouseMoveEvent(self, event):
        if (
            self._manual_drag_start_pos is not None
            and event.buttons() & Qt.LeftButton
            and self._manual_drag_payload
        ):
            distance = (self._event_pos(event) - self._manual_drag_start_pos).manhattanLength()
            if distance >= QApplication.startDragDistance():
                self._manual_drag_active = True
                self.setCursor(Qt.ClosedHandCursor)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._manual_drag_active and self._manual_drag_payload:
            target = self._workflow_drop_target_at(self._event_global_pos(event))
            payload = self._manual_drag_payload
            self._reset_manual_drag()
            if target is not None and target is not self:
                mime = QMimeData()
                mime.setData(MIME_WORKFLOW_ITEMS, json.dumps(payload).encode("utf-8"))
                mime.setText("\n".join(str(entry["disk_name"]) for entry in payload.get("items") or []))
                proxy = _WorkflowDropProxy(self, mime)
                target.dropEvent(proxy)
                if proxy.accepted:
                    event.accept()
                    return
        self._reset_manual_drag()
        super().mouseReleaseEvent(event)

    @staticmethod
    def _event_pos(event):
        if hasattr(event, "position"):
            return event.position().toPoint()
        return event.pos()

    @staticmethod
    def _event_global_pos(event):
        if hasattr(event, "globalPosition"):
            return event.globalPosition().toPoint()
        return event.globalPos()

    def _reset_manual_drag(self) -> None:
        self._manual_drag_start_pos = None
        self._manual_drag_payload = None
        self._manual_drag_active = False
        self.unsetCursor()

    @staticmethod
    def _workflow_drop_target_at(global_pos) -> Optional["DragListWidget"]:
        widget = QApplication.widgetAt(global_pos)
        while widget is not None:
            if isinstance(widget, DragListWidget):
                return widget
            widget = widget.parentWidget()
        best_widget = None
        best_distance = None
        for candidate in QApplication.allWidgets():
            if not isinstance(candidate, DragListWidget) or not candidate.isVisible():
                continue
            rect = candidate.rect()
            top_left = candidate.mapToGlobal(rect.topLeft())
            bottom_right = candidate.mapToGlobal(rect.bottomRight())
            if top_left.x() <= global_pos.x() <= bottom_right.x():
                if top_left.y() <= global_pos.y() <= bottom_right.y():
                    return candidate
                distance = min(abs(global_pos.y() - top_left.y()), abs(global_pos.y() - bottom_right.y()))
                if distance <= 140 and (best_distance is None or distance < best_distance):
                    best_widget = candidate
                    best_distance = distance
        if best_widget is not None:
            return best_widget
        return None

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.source() is not self:
            event.setDropAction(Qt.MoveAction)
            event.accept()

    def dragMoveEvent(self, event: QDragEnterEvent):
        if event.source() is not self:
            event.setDropAction(Qt.MoveAction)
            event.accept()

    def startDrag(self, supportedActions):
        items = self._iter_source_items(self)
        payload = self._workflow_drag_payload(items)
        if not payload["items"]:
            return
        mime = QMimeData()
        mime.setData(MIME_WORKFLOW_ITEMS, json.dumps(payload).encode("utf-8"))
        mime.setText("\n".join(str(entry["disk_name"]) for entry in payload["items"]))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)

    @staticmethod
    def _iter_source_items(source) -> list[QListWidgetItem]:
        selected = []
        if hasattr(source, "selectedItems"):
            selected = [it for it in source.selectedItems() if it is not None]
        if not selected and hasattr(source, "currentItem"):
            current = source.currentItem()
            if current is not None:
                selected = [current]
        return selected

    @staticmethod
    def _find_source_item(source, item_id: Optional[int], disk_name: str) -> Optional[QListWidgetItem]:
        if not hasattr(source, "count") or not hasattr(source, "item"):
            return None
        disk_name = str(disk_name or "").strip()
        for row in range(source.count()):
            item = source.item(row)
            if item is None:
                continue
            if item_id is not None:
                try:
                    if int(item.data(ROLE_DB_ID) or 0) == int(item_id):
                        return item
                except Exception:
                    pass
            if disk_name and str(item.data(ROLE_DISK_NAME) or "").strip() == disk_name:
                return item
        return None

    def _workflow_drag_payload(self, items: list[QListWidgetItem]) -> dict:
        payload_items: list[dict] = []
        for item in items:
            disk_name = str(item.data(ROLE_DISK_NAME) or item.data(Qt.UserRole) or item.text() or "").strip()
            if not disk_name:
                continue
            raw_id = item.data(ROLE_DB_ID)
            item_id = None
            if raw_id is not None:
                try:
                    item_id = int(raw_id)
                except Exception:
                    item_id = None
            payload_items.append(
                {
                    "item_id": item_id,
                    "disk_name": disk_name,
                    "text": str(item.text() or disk_name),
                }
            )
        return {
            "domain": str(self.workflow_domain or "").strip().lower(),
            "stage": int(self.workflow_stage or 0),
            "items": payload_items,
        }

    def _workflow_rows_from_event(self, event: QDropEvent, source) -> list[tuple[Optional[QListWidgetItem], Optional[int], str]]:
        mime = event.mimeData()
        if mime and mime.hasFormat(MIME_WORKFLOW_ITEMS):
            try:
                payload = json.loads(bytes(mime.data(MIME_WORKFLOW_ITEMS)).decode("utf-8"))
            except Exception:
                payload = {}
            rows: list[tuple[Optional[QListWidgetItem], Optional[int], str]] = []
            for entry in payload.get("items") or []:
                disk_name = str((entry or {}).get("disk_name") or "").strip()
                if not disk_name:
                    continue
                item_id = None
                raw_id = (entry or {}).get("item_id")
                if raw_id is not None:
                    try:
                        item_id = int(raw_id)
                    except Exception:
                        item_id = None
                rows.append((self._find_source_item(source, item_id, disk_name), item_id, disk_name))
            if rows:
                return rows

        rows = []
        for item in self._iter_source_items(source):
            item_id = self._resolve_item_id(item)
            disk_name = str(item.data(ROLE_DISK_NAME) or item.data(Qt.UserRole) or "").strip()
            rows.append((item, item_id, disk_name))
        return rows

    def _resolve_item_id(self, item: QListWidgetItem) -> Optional[int]:
        item_id = item.data(ROLE_DB_ID)
        if item_id is not None:
            try:
                return int(item_id)
            except Exception:
                return None

        if self.db is None:
            return None

        disk_name = item.data(ROLE_DISK_NAME) or item.data(Qt.UserRole)
        if not disk_name:
            return None

        try:
            db_item = self.db.get_item_by_disk_name(
                str(disk_name),
                domain=self.workflow_domain,
            )
        except Exception:
            return None
        return int(db_item.id) if db_item else None

    def dropEvent(self, event: QDropEvent):
        source = event.source()
        if not source or source is self:
            return

        # Unified mode: update DB domain/stage only (no disk rename).
        if self.db is not None and self.workflow_domain and self.workflow_stage is not None:
            source_rows = self._workflow_rows_from_event(event, source)
            if not source_rows:
                return

            moved = []
            errors = []

            source_day_name = getattr(source, "day_name", "") or ""
            try:
                old_stage = int(getattr(source, "workflow_stage", 0) or 0)
            except Exception:
                old_stage = 0

            for item, item_id, raw_name in source_rows:
                raw_name = str(raw_name or "").strip()
                if not raw_name:
                    label = item.text() if item is not None else "Selected row"
                    errors.append(f"{label}: missing folder name")
                    continue

                if source_day_name == "3. Edit" and self.day_name != "3. Edit":
                    if item_id is None:
                        errors.append(f"{raw_name}: missing DB id for Edit gate check")
                        continue
                    try:
                        db_item = self.db.get_item_by_id(int(item_id))
                    except Exception as exc:
                        errors.append(f"{raw_name}: failed to read I/G flags ({exc})")
                        continue
                    if not db_item or not (bool(db_item.flag_i) and bool(db_item.flag_g)):
                        errors.append(f"{raw_name}: requires both I and G to leave Edit")
                        continue

                try:
                    if item_id is not None:
                        self.db.update_domain_stage(
                            int(item_id),
                            domain=self.workflow_domain,
                            stage=int(self.workflow_stage),
                        )
                        moved.append((item, raw_name, int(item_id)))
                    else:
                        new_id = int(
                            self.db.upsert_into_domain(
                                disk_name=raw_name,
                                domain=self.workflow_domain,
                                stage=int(self.workflow_stage),
                            )
                        )
                        moved.append((item, raw_name, new_id))
                except Exception as exc:
                    errors.append(f"{raw_name}: {exc}")

            for item, _, moved_item_id in sorted(
                (row for row in moved if row[0] is not None),
                key=lambda row: source.row(row[0]),
                reverse=True,
            ):
                row_index = source.row(item)
                if row_index >= 0:
                    source.takeItem(row_index)
                if source_day_name == "3. Edit" and self.day_name != "3. Edit":
                    try:
                        self.itemRemovedFromEdit.emit(int(moved_item_id))
                    except Exception:
                        pass

            if moved:
                for _, final_name, moved_item_id in moved:
                    self.add_entry(
                        final_name,
                        _extract_suffix_by_day_name(final_name, self.day_name),
                        item_id=moved_item_id,
                    )
                    row_item = self.item(self.count() - 1)
                    if row_item is not None:
                        self.apply_moved_style(row_item, True)
                    self.folderDroppedDetailed.emit(int(moved_item_id), int(old_stage), int(self.workflow_stage or 0), str(final_name))
                event.setDropAction(Qt.MoveAction)
                event.accept()
                self.folderDropped.emit()
                self.uiReloadRequested.emit()

            if errors:
                QMessageBox.warning(self, "Move Failed", "\n".join(errors[:8]))
            return

        # Legacy fallback (filesystem rename) for contexts without DB wiring.
        item = source.currentItem() if hasattr(source, "currentItem") else None
        if not item:
            return

        if getattr(source, "day_name", "") == "3. Edit" and self.day_name != "3. Edit":
            folder_name = item.data(ROLE_DISK_NAME)
            has_i = bool(re.search(r"\bI\b", folder_name or ""))
            has_g = bool(re.search(r"\bG\b", folder_name or ""))
            if not (has_i and has_g):
                QMessageBox.warning(self, "Move Blocked", "You must check both I and G before moving this folder out of '3. Edit'.")
                return

        row = source.row(item)
        orig_name = item.data(ROLE_DISK_NAME)

        clean_name = re.sub(r" ip\([^)]*\)", "", orig_name)
        if getattr(source, "day_name", "") == "3. Edit" and self.day_name != "3. Edit":
            clean_name = re.sub(r"\bI\b", "", clean_name)
            clean_name = re.sub(r"\bG\b", "", clean_name)
            clean_name = re.sub(r"\s{2,}", " ", clean_name).strip()

        suffix = _extract_suffix_by_day_name(clean_name, getattr(source, "day_name", ""))
        new_name = f"{self.day_name} {suffix}".strip()

        old_path = os.path.join(self.base_dir, orig_name)
        new_path = os.path.join(self.base_dir, new_name)

        try:
            os.rename(old_path, new_path)
        except Exception as e:
            QMessageBox.warning(self, "Rename Failed", f"{old_path} → {new_path}\n{e}")
            return

        new_item = QListWidgetItem(_extract_suffix_by_day_name(new_name, self.day_name))
        new_item.setData(ROLE_DISK_NAME, new_name)
        self.addItem(new_item)

        source.takeItem(row)
        if getattr(source, "day_name", "") == "3. Edit" and self.day_name != "3. Edit":
            removed_id = item.data(ROLE_DB_ID)
            if removed_id is not None:
                try:
                    self.itemRemovedFromEdit.emit(int(removed_id))
                except Exception:
                    pass

        event.acceptProposedAction()
        self.folderDropped.emit()
        self.uiReloadRequested.emit()

    def show_context_menu(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return

        item = self.item(index.row())
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2e2e2e;
                color: white;
                border: 1px solid #444;
            }
            QMenu::item {
                padding: 6px 20px;
                background-color: transparent;
            }
            QMenu::item:selected {
                background-color: #444;
            }
        """)

        if self.db is not None and self.workflow_domain:
            action_set_ip = menu.addAction("Set In Progress")
            action_clear_ip = menu.addAction("Clear In Progress")
            chosen = menu.exec(self.viewport().mapToGlobal(pos))

            if chosen not in {action_set_ip, action_clear_ip}:
                return

            item_id = self._resolve_item_id(item)
            if item_id is None:
                QMessageBox.warning(self, "In Progress", "This item does not have a valid DB id.")
                return

            if chosen == action_set_ip:
                dialog = QInputDialog(self)
                dialog.setWindowTitle("In Progress")
                dialog.setLabelText("Name of person working on it:")
                dialog.setStyleSheet("""
                    QInputDialog {
                        background-color: #2e2e2e;
                        color: white;
                    }
                    QLineEdit {
                        background-color: #3a3a3a;
                        color: white;
                        border: 1px solid #555;
                    }
                    QLabel {
                        color: white;
                    }
                """)
                ok = dialog.exec()
                name: Optional[str] = dialog.textValue().strip() if ok else None
                if not ok or not name:
                    return
                try:
                    self.db.set_in_progress(int(item_id), name)
                except Exception as exc:
                    QMessageBox.warning(self, "In Progress", f"Failed to update DB:\n{exc}")
                    return
                self.apply_in_progress_style(item, name)
            else:
                try:
                    self.db.set_in_progress(int(item_id), None)
                except Exception as exc:
                    QMessageBox.warning(self, "In Progress", f"Failed to update DB:\n{exc}")
                    return
                self.apply_in_progress_style(item, None)

            self.uiReloadRequested.emit()
            return

        action_ip = menu.addAction("In Progress")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen == action_ip:
            dialog = QInputDialog(self)
            dialog.setWindowTitle("In Progress")
            dialog.setLabelText("Name of person working on it:")
            dialog.setStyleSheet("""
                QInputDialog {
                    background-color: #2e2e2e;
                    color: white;
                }
                QLineEdit {
                    background-color: #3a3a3a;
                    color: white;
                    border: 1px solid #555;
                }
                QLabel {
                    color: white;
                }
            """)
            ok = dialog.exec()
            name: Optional[str] = dialog.textValue().strip() if ok else None

            if not ok or not name:
                return

            orig_name = item.data(ROLE_DISK_NAME)
            clean_name = re.sub(r" ip\([^)]*\)", "", orig_name)
            suffix = _extract_suffix_by_day_name(clean_name, self.day_name)
            ip_tag = f"ip({name})"
            new_name = f"{self.day_name} {ip_tag} {suffix}".strip()

            old_path = os.path.join(self.base_dir, orig_name)
            new_path = os.path.join(self.base_dir, new_name)

            try:
                os.rename(old_path, new_path)
            except Exception as e:
                QMessageBox.warning(self, "Rename Failed", f"{old_path} → {new_path}\n\n{e}")
                return

            item.setData(ROLE_DISK_NAME, new_name)
            item.setText(_extract_suffix_by_day_name(new_name, self.day_name))
            item.setBackground(QBrush(QColor(255, 165, 0)))
            item.setForeground(QBrush(QColor(0, 0, 0)))

    def open_folder(self, item: QListWidgetItem) -> None:
        disk_name = str(item.data(ROLE_DISK_NAME) or "").strip()
        candidates = self._candidate_open_paths(disk_name)
        path = candidates[0] if candidates else ""
        for candidate in candidates:
            if os.path.isdir(candidate):
                path = candidate
                break
        if not os.path.isdir(path):
            QMessageBox.warning(self, "Open Folder Failed", f"Folder does not exist:\n{path}")
            return
        try:
            if sys.platform.startswith('win'):
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open' if sys.platform.startswith('linux') else 'open', path])
        except Exception as e:
            QMessageBox.warning(self, "Open Folder Failed", f"Could not open:\n{path}\n{e}")
