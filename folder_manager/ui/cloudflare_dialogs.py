from __future__ import annotations

import os
from typing import Dict, Optional

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from folder_manager.sms.cloudflare_r2 import missing_r2_settings
from folder_manager.ui.sms_dialogs import SETTINGS_APP, SETTINGS_ORG

KEY_ACCOUNT_ID = "sms/r2/account_id"
KEY_ACCESS_KEY_ID = "sms/r2/access_key_id"
KEY_SECRET_ACCESS_KEY = "sms/r2/secret_access_key"
KEY_BUCKET_NAME = "sms/r2/bucket_name"
KEY_PUBLIC_BASE_URL = "sms/r2/public_base_url"


def _settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def load_r2_settings() -> Dict[str, str]:
    settings = _settings()
    return {
        "account_id": str(settings.value(KEY_ACCOUNT_ID, "") or "").strip()
        or str(os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip(),
        "access_key_id": str(settings.value(KEY_ACCESS_KEY_ID, "") or "").strip()
        or str(os.getenv("R2_ACCESS_KEY_ID") or "").strip(),
        "secret_access_key": str(settings.value(KEY_SECRET_ACCESS_KEY, "") or "").strip()
        or str(os.getenv("R2_SECRET_ACCESS_KEY") or "").strip(),
        "bucket_name": str(settings.value(KEY_BUCKET_NAME, "") or "").strip()
        or str(os.getenv("R2_BUCKET_NAME") or "").strip(),
        "public_base_url": str(settings.value(KEY_PUBLIC_BASE_URL, "") or "").strip()
        or str(os.getenv("R2_PUBLIC_BASE_URL") or "").strip(),
    }


def save_r2_settings(values: Dict[str, str]) -> None:
    settings = _settings()
    settings.setValue(KEY_ACCOUNT_ID, str(values.get("account_id") or "").strip())
    settings.setValue(KEY_ACCESS_KEY_ID, str(values.get("access_key_id") or "").strip())
    settings.setValue(KEY_SECRET_ACCESS_KEY, str(values.get("secret_access_key") or "").strip())
    settings.setValue(KEY_BUCKET_NAME, str(values.get("bucket_name") or "").strip())
    settings.setValue(KEY_PUBLIC_BASE_URL, str(values.get("public_base_url") or "").strip().rstrip("/"))
    settings.sync()


class CloudflareR2SettingsDialog(QDialog):
    def __init__(self, parent=None, *, current: Optional[Dict[str, str]] = None):
        super().__init__(parent)
        self.setWindowTitle("Cloudflare R2 Settings")
        self.setModal(True)
        self.setMinimumWidth(620)

        values = current or load_r2_settings()
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Enter Cloudflare R2 settings for this computer. The app uses R2 to host MMS preview images."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.account_edit = QLineEdit(self)
        self.account_edit.setText(str(values.get("account_id") or "").strip())
        self.account_edit.setPlaceholderText("Cloudflare Account ID")
        form.addRow("Account ID:", self.account_edit)

        self.access_key_edit = QLineEdit(self)
        self.access_key_edit.setText(str(values.get("access_key_id") or "").strip())
        form.addRow("R2 Access Key ID:", self.access_key_edit)

        self.secret_key_edit = QLineEdit(self)
        self.secret_key_edit.setText(str(values.get("secret_access_key") or "").strip())
        self.secret_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("R2 Secret Access Key:", self.secret_key_edit)

        self.show_secret_check = QCheckBox("Show secret key", self)
        self.show_secret_check.toggled.connect(self._set_secret_visible)
        form.addRow("", self.show_secret_check)

        self.bucket_edit = QLineEdit(self)
        self.bucket_edit.setText(str(values.get("bucket_name") or "").strip())
        self.bucket_edit.setPlaceholderText("bucket-name")
        form.addRow("Bucket Name:", self.bucket_edit)

        self.public_url_edit = QLineEdit(self)
        self.public_url_edit.setText(str(values.get("public_base_url") or "").strip())
        self.public_url_edit.setPlaceholderText("https://your-public-domain.example.com")
        form.addRow("Public Base URL:", self.public_url_edit)
        layout.addLayout(form)

        note = QLabel(
            "The Public Base URL must open R2 objects in a browser. Twilio MMS cannot use private R2 URLs."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_secret_visible(self, checked: bool) -> None:
        self.secret_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def values(self) -> Dict[str, str]:
        return {
            "account_id": (self.account_edit.text() or "").strip(),
            "access_key_id": (self.access_key_edit.text() or "").strip(),
            "secret_access_key": (self.secret_key_edit.text() or "").strip(),
            "bucket_name": (self.bucket_edit.text() or "").strip(),
            "public_base_url": (self.public_url_edit.text() or "").strip().rstrip("/"),
        }

    def accept(self) -> None:  # type: ignore[override]
        values = self.values()
        missing = missing_r2_settings(values)
        if missing:
            QMessageBox.warning(self, "Cloudflare R2 Settings", "Please fill in: " + ", ".join(missing))
            return
        save_r2_settings(values)
        super().accept()


def ensure_r2_settings(parent=None) -> Optional[Dict[str, str]]:
    values = load_r2_settings()
    if not missing_r2_settings(values):
        return values
    dialog = CloudflareR2SettingsDialog(parent, current=values)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.values()
