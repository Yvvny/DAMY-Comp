from __future__ import annotations

import hashlib
from typing import Callable

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

_GLOBAL_OPEN_TOP_DIALOGS: dict[str, QDialog] = {}
_GLOBAL_ACTIVE_TOP_DIALOG: QDialog | None = None


def dialog_dedupe_key(dlg: QDialog) -> str:
    custom_key = dlg.property("dialog_dedupe_key")
    if custom_key is not None:
        return str(custom_key).strip().lower()
    title = str(dlg.windowTitle() or "").strip().lower()
    class_name = dlg.metaObject().className() if dlg.metaObject() is not None else dlg.__class__.__name__
    return f"{class_name}|{title}"


def make_dialog_dedupe_key(namespace: str, *parts: object) -> str:
    payload = "|".join(str(part or "") for part in parts)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{str(namespace or 'dialog').strip().lower()}|{digest}"


def center_dialog(dlg: QDialog, anchor: QWidget | None = None) -> None:
    dlg.adjustSize()

    target = anchor
    if target is None:
        parent = dlg.parentWidget()
        if parent is not None and parent.isVisible():
            target = parent

    if target is not None and target.isVisible():
        px = target.x() + max(0, (target.width() - dlg.width()) // 2)
        py = target.y() + max(0, (target.height() - dlg.height()) // 2)
        dlg.move(px, py)
        return

    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    if screen is None:
        return
    geo = screen.availableGeometry()
    px = geo.x() + max(0, (geo.width() - dlg.width()) // 2)
    py = geo.y() + max(0, (geo.height() - dlg.height()) // 2)
    dlg.move(px, py)


def show_dialog_topmost_non_modal(
    dlg: QDialog,
    *,
    anchor: QWidget | None = None,
    open_dialogs: dict[str, QDialog] | None = None,
    set_active_dialog: Callable[[QDialog], None] | None = None,
    clear_active_dialog: Callable[[QDialog], None] | None = None,
) -> int:
    global _GLOBAL_ACTIVE_TOP_DIALOG

    registry = open_dialogs if open_dialogs is not None else _GLOBAL_OPEN_TOP_DIALOGS
    dlg_key = dialog_dedupe_key(dlg)
    existing = registry.get(dlg_key)
    existing_visible = False
    if existing is not None and existing is not dlg:
        try:
            existing_visible = bool(existing.isVisible())
        except Exception:
            if registry.get(dlg_key) is existing:
                registry.pop(dlg_key, None)
            existing = None
    if existing is not None and existing_visible:
        result = {"code": int(QDialog.DialogCode.Rejected)}
        loop = QEventLoop()

        def _on_existing_finished(code: int) -> None:
            result["code"] = int(code)
            if loop.isRunning():
                loop.quit()

        existing.finished.connect(_on_existing_finished)
        try:
            existing.raise_()
            existing.activateWindow()
            loop.exec()
            return int(result["code"])
        finally:
            try:
                existing.finished.disconnect(_on_existing_finished)
            except Exception:
                pass

    result = {"code": int(QDialog.DialogCode.Rejected)}
    loop = QEventLoop()

    def _on_finished(code: int) -> None:
        result["code"] = int(code)
        if loop.isRunning():
            loop.quit()

    dlg.finished.connect(_on_finished)
    registry[dlg_key] = dlg

    def _cleanup_dialog_entry(_code: int) -> None:
        if registry.get(dlg_key) is dlg:
            registry.pop(dlg_key, None)

    dlg.finished.connect(_cleanup_dialog_entry)
    dlg.setModal(False)
    dlg.setWindowModality(Qt.NonModal)
    dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    center_dialog(dlg, anchor)

    if set_active_dialog is not None:
        set_active_dialog(dlg)
    else:
        _GLOBAL_ACTIVE_TOP_DIALOG = dlg

    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    try:
        loop.exec()
        return int(result["code"])
    finally:
        if clear_active_dialog is not None:
            clear_active_dialog(dlg)
        elif _GLOBAL_ACTIVE_TOP_DIALOG is dlg:
            _GLOBAL_ACTIVE_TOP_DIALOG = None


def show_message_box_topmost_non_modal(
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
    open_dialogs: dict[str, QDialog] | None = None,
    set_active_dialog: Callable[[QDialog], None] | None = None,
    clear_active_dialog: Callable[[QDialog], None] | None = None,
) -> int:
    dlg = QMessageBox(parent)
    dlg.setIcon(icon)
    dlg.setWindowTitle(str(title or ""))
    dlg.setText(str(text or ""))
    if informative_text:
        dlg.setInformativeText(str(informative_text))
    if detailed_text:
        dlg.setDetailedText(str(detailed_text))
    dlg.setStandardButtons(buttons)
    dlg.setDefaultButton(default_button)

    for raw_key, label in (button_labels or {}).items():
        try:
            std_button = QMessageBox.StandardButton(int(raw_key))
        except Exception:
            continue
        btn = dlg.button(std_button)
        if btn is not None:
            btn.setText(str(label))

    if dedupe_key:
        key = str(dedupe_key).strip().lower()
    else:
        key = make_dialog_dedupe_key(
            "msgbox",
            int(icon),
            str(title or "").strip().lower(),
            str(text or "").strip(),
            int(buttons),
            int(default_button),
        )
    dlg.setProperty("dialog_dedupe_key", key)
    return show_dialog_topmost_non_modal(
        dlg,
        anchor=parent,
        open_dialogs=open_dialogs,
        set_active_dialog=set_active_dialog,
        clear_active_dialog=clear_active_dialog,
    )


def _find_message_box_delegate(widget: QWidget | None):
    current = widget
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        handler = getattr(current, "_show_message_box_topmost_non_modal", None)
        if callable(handler):
            return handler
        current = current.parentWidget()
    return None


def show_message_box_unified(
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
    delegated = _find_message_box_delegate(parent)
    if delegated is not None:
        return int(
            delegated(
                icon,
                title,
                text,
                parent=parent,
                buttons=buttons,
                default_button=default_button,
                button_labels=button_labels,
                informative_text=informative_text,
                detailed_text=detailed_text,
                dedupe_key=dedupe_key,
            )
        )
    return show_message_box_topmost_non_modal(
        icon,
        title,
        text,
        parent=parent,
        buttons=buttons,
        default_button=default_button,
        button_labels=button_labels,
        informative_text=informative_text,
        detailed_text=detailed_text,
        dedupe_key=dedupe_key,
    )


def show_warning_unified(parent: QWidget | None, title: str, text: str) -> int:
    return show_message_box_unified(QMessageBox.Icon.Warning, title, text, parent=parent)


def show_information_unified(parent: QWidget | None, title: str, text: str) -> int:
    return show_message_box_unified(QMessageBox.Icon.Information, title, text, parent=parent)


def show_critical_unified(parent: QWidget | None, title: str, text: str) -> int:
    return show_message_box_unified(QMessageBox.Icon.Critical, title, text, parent=parent)
