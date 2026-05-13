from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)


SETTINGS_ORG = "DAMYComp"
SETTINGS_APP = "DAMYComp"
KEY_ACCOUNT_SID = "sms/twilio/account_sid"
KEY_AUTH_TOKEN = "sms/twilio/auth_token"
KEY_FROM_NUMBER = "sms/twilio/from_number"


def _settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def load_twilio_settings() -> Dict[str, str]:
    settings = _settings()
    return {
        "account_sid": str(settings.value(KEY_ACCOUNT_SID, "") or "").strip()
        or str(os.getenv("TWILIO_ACCOUNT_SID") or "").strip(),
        "auth_token": str(settings.value(KEY_AUTH_TOKEN, "") or "").strip()
        or str(os.getenv("TWILIO_AUTH_TOKEN") or "").strip(),
        "from_number": str(settings.value(KEY_FROM_NUMBER, "") or "").strip()
        or str(os.getenv("TWILIO_FROM_NUMBER") or "").strip(),
    }


def save_twilio_settings(values: Dict[str, str]) -> None:
    settings = _settings()
    settings.setValue(KEY_ACCOUNT_SID, str(values.get("account_sid") or "").strip())
    settings.setValue(KEY_AUTH_TOKEN, str(values.get("auth_token") or "").strip())
    settings.setValue(KEY_FROM_NUMBER, str(values.get("from_number") or "").strip())
    settings.sync()


def missing_twilio_settings(values: Dict[str, str]) -> Tuple[str, ...]:
    missing = []
    if not str(values.get("account_sid") or "").strip():
        missing.append("Account SID")
    if not str(values.get("auth_token") or "").strip():
        missing.append("Auth Token")
    if not str(values.get("from_number") or "").strip():
        missing.append("From Number")
    return tuple(missing)


class TwilioSettingsDialog(QDialog):
    def __init__(self, parent=None, *, current: Optional[Dict[str, str]] = None):
        super().__init__(parent)
        self.setWindowTitle("Twilio SMS Settings")
        self.setModal(True)
        self.setMinimumWidth(520)

        values = current or load_twilio_settings()
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Enter Twilio settings for this computer. These settings are saved locally and survive app updates."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.sid_edit = QLineEdit(self)
        self.sid_edit.setText(str(values.get("account_sid") or "").strip())
        self.sid_edit.setPlaceholderText("ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        form.addRow("Account SID:", self.sid_edit)

        self.token_edit = QLineEdit(self)
        self.token_edit.setText(str(values.get("auth_token") or "").strip())
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Auth Token:", self.token_edit)

        self.show_token_check = QCheckBox("Show Auth Token", self)
        self.show_token_check.toggled.connect(self._set_token_visible)
        form.addRow("", self.show_token_check)

        self.from_edit = QLineEdit(self)
        self.from_edit.setText(str(values.get("from_number") or "").strip())
        self.from_edit.setPlaceholderText("+19297349818")
        form.addRow("From Number:", self.from_edit)
        layout.addLayout(form)

        warning = QLabel("Do not share your Auth Token. Anyone with it can send messages from your account.")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_token_visible(self, checked: bool) -> None:
        self.token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def values(self) -> Dict[str, str]:
        return {
            "account_sid": (self.sid_edit.text() or "").strip(),
            "auth_token": (self.token_edit.text() or "").strip(),
            "from_number": (self.from_edit.text() or "").strip(),
        }

    def accept(self) -> None:  # type: ignore[override]
        values = self.values()
        missing = missing_twilio_settings(values)
        if missing:
            QMessageBox.warning(
                self,
                "Twilio SMS Settings",
                "Please fill in: " + ", ".join(missing),
            )
            return
        save_twilio_settings(values)
        super().accept()


class SmsPreviewDialog(QDialog):
    def __init__(self, parent=None, *, to_phone: str, body: str):
        super().__init__(parent)
        self.setWindowTitle("Send Text")
        self.setModal(True)
        self.resize(620, 420)
        self.setMinimumSize(520, 320)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.to_edit = QLineEdit(self)
        self.to_edit.setText(str(to_phone or "").strip())
        form.addRow("To:", self.to_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("Message:"))
        self.body_edit = QPlainTextEdit(self)
        self.body_edit.setPlainText(str(body or "").strip())
        self.body_edit.setMinimumHeight(180)
        layout.addWidget(self.body_edit, 1)

        hint = QLabel("Keep text short. Long SMS messages can be billed as multiple segments.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok,
            self,
        )
        send_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if send_button is not None:
            send_button.setText("Send")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def sms_values(self) -> Tuple[str, str]:
        return (
            (self.to_edit.text() or "").strip(),
            (self.body_edit.toPlainText() or "").strip(),
        )


def ensure_twilio_settings(parent=None) -> Optional[Dict[str, str]]:
    values = load_twilio_settings()
    if not missing_twilio_settings(values):
        return values
    dialog = TwilioSettingsDialog(parent, current=values)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.values()

