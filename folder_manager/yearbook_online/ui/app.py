import sys
from PySide6.QtWidgets import QApplication

from ..config import BASE_DIRECTORY
from ...config import DB_HOST, DB_NAME, DB_USER, DB_PASS, DB_PORT
from ...migrations import run_migrations
from .style import apply_dark_theme
from .views import DragDropFolders


def run_app() -> None:
    conninfo = (
        f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
        f"user={DB_USER} password={DB_PASS}"
    )
    run_migrations(conninfo)
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    window = DragDropFolders(str(BASE_DIRECTORY))
    window.show()
    sys.exit(app.exec())
