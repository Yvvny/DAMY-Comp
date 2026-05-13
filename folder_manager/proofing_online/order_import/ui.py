from __future__ import annotations

import os
import sys
from typing import List, Optional

from googleapiclient.errors import HttpError
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .exceptions import NoOrdersFoundError
from .gmail_client import get_gmail_service
from .parsers import parse_picture_day_ids
from .processing import combine_selected_order_pdfs, process_picture_day
from .utils import emit_status


class OrderProcessingWorker(QObject):
    progress = pyqtSignal(str)
    fatal_error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, picture_day_ids: List[str], order_type: str):
        super().__init__()
        self.picture_day_ids = picture_day_ids
        self.order_type = order_type

    def run(self):
        try:
            emit_status('Initializing Gmail service...', self.progress.emit)
            service = get_gmail_service()
            emit_status('Gmail service ready.', self.progress.emit)
        except Exception as exc:
            self.fatal_error.emit(f'Failed to initialize Gmail service: {exc}')
            self.finished.emit()
            return

        for picture_day_id in self.picture_day_ids:
            try:
                process_picture_day(
                    service,
                    picture_day_id,
                    self.order_type,
                    progress_callback=self.progress.emit,
                )
                emit_status(f'{picture_day_id}: Processing complete!', self.progress.emit)
            except NoOrdersFoundError as error:
                emit_status(str(error), self.progress.emit)
            except HttpError as error:
                emit_status(f'{picture_day_id}: Gmail API error - {error}', self.progress.emit)
            except Exception as error:
                emit_status(f'{picture_day_id}: Unexpected error - {error}', self.progress.emit)

        self.finished.emit()


class OrderImportWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Order Import')
        self.setMinimumWidth(500)

        self.picture_day_input = QPlainTextEdit()
        self.picture_day_input.setPlaceholderText(
            'Enter Picture Day IDs (one per line or separated by commas).'
        )
        self.picture_day_input.setTabChangesFocus(True)

        order_type_label = QLabel('Order Source:')
        order_type_label.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.godaddy_radio = QRadioButton('GoDaddy')
        self.godaddy_radio.setChecked(True)
        self.photodeck_radio = QRadioButton('PhotoDeck')

        self.order_type_group = QButtonGroup(self)
        self.order_type_group.addButton(self.godaddy_radio)
        self.order_type_group.addButton(self.photodeck_radio)

        order_type_layout = QHBoxLayout()
        order_type_layout.addWidget(order_type_label)
        order_type_layout.addWidget(self.godaddy_radio)
        order_type_layout.addWidget(self.photodeck_radio)
        order_type_layout.addStretch()

        self.run_button = QPushButton('Run')
        self.run_button.clicked.connect(self.on_run_clicked)

        self.combine_button = QPushButton('Combine Selected PDFs')
        self.combine_button.clicked.connect(self.on_combine_pdfs_clicked)

        self.status_output = QTextEdit()
        self.status_output.setReadOnly(True)
        self.status_output.setPlaceholderText('Status updates will appear here.')

        layout = QVBoxLayout()
        layout.addWidget(QLabel('Picture Day IDs:'))
        layout.addWidget(self.picture_day_input)
        layout.addLayout(order_type_layout)
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.run_button)
        button_layout.addWidget(self.combine_button)
        button_layout.addStretch()
        layout.addLayout(button_layout)
        layout.addWidget(QLabel('Status:'))
        layout.addWidget(self.status_output)

        self.setLayout(layout)

        self.thread: Optional[QThread] = None
        self.worker: Optional[OrderProcessingWorker] = None

    def append_status(self, message: str) -> None:
        self.status_output.append(message)

    def on_run_clicked(self):
        try:
            picture_day_ids = parse_picture_day_ids(self.picture_day_input.toPlainText())
        except ValueError as exc:
            QMessageBox.warning(self, 'Invalid Input', str(exc))
            return

        if not picture_day_ids:
            QMessageBox.information(self, 'Input Required', 'Please enter at least one Picture Day ID.')
            return

        order_type = 'godaddy' if self.godaddy_radio.isChecked() else 'photodeck'
        self.run_button.setEnabled(False)
        self.status_output.clear()
        self.append_status(f'Starting import for {len(picture_day_ids)} Picture Day ID(s)...')

        self.thread = QThread()
        self.worker = OrderProcessingWorker(picture_day_ids, order_type)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.append_status)
        self.worker.fatal_error.connect(self.on_fatal_error)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self.thread.start()

    def on_fatal_error(self, message: str):
        self.append_status(message)
        QMessageBox.critical(self, 'Error', message)

    def on_worker_finished(self):
        self.append_status('Processing complete.')
        self.run_button.setEnabled(True)
        self.thread = None
        self.worker = None

    def on_combine_pdfs_clicked(self):
        selected_files, _ = QFileDialog.getOpenFileNames(
            self,
            'Select Order PDFs',
            '',
            'PDF Files (*.pdf)',
        )
        if not selected_files:
            return

        initial_dir = os.path.dirname(selected_files[0]) if selected_files else ''
        default_name = os.path.join(initial_dir, 'Combined Orders.pdf') if initial_dir else 'Combined Orders.pdf'
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            'Save Combined PDF',
            default_name,
            'PDF Files (*.pdf)',
        )
        if not output_path:
            return

        if not output_path.lower().endswith('.pdf'):
            output_path += '.pdf'

        try:
            ordered_paths = combine_selected_order_pdfs(selected_files, output_path)
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.critical(self, 'Combine Failed', f'Unable to combine PDFs: {exc}')
            return

        self.append_status(f'Combined {len(ordered_paths)} PDF(s) into {output_path}.')
        for path in ordered_paths:
            self.append_status(f'   - {os.path.basename(path)}')

    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            self.append_status('Stopping background worker...')
            self.thread.quit()
            self.thread.wait()
        super().closeEvent(event)


def run_app():
    app = QApplication(sys.argv)
    window = OrderImportWindow()
    window.show()
    sys.exit(app.exec())
