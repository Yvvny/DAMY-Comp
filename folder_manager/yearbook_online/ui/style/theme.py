from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import Qt

def apply_dark_theme(app):
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(53, 53, 53))
    pal.setColor(QPalette.WindowText, Qt.white)
    pal.setColor(QPalette.Base, QColor(35, 35, 35))
    pal.setColor(QPalette.Text, Qt.white)
    pal.setColor(QPalette.Button, QColor(53, 53, 53))
    pal.setColor(QPalette.Highlight, QColor(42, 130, 218))
    pal.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(pal)
