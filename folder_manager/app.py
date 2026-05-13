import sys
import tempfile
import threading
import traceback
import signal
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .ui.main_window import DragDropFolders
from .ui.dialog_system import show_critical_unified
from .utils.theme import apply_dark_theme
from .migrations import run_migrations, ensure_base_directory
from .config import DB_HOST, DB_NAME, DB_USER, DB_PASS, DB_PORT, BASE_DIRECTORY

CONNINFO = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASS}"
)


def _append_ui_runtime_log(message: str) -> None:
    try:
        log_path = Path(tempfile.gettempdir()) / "DAMYComp_ui_runtime.log"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def _write_startup_error_log(err: Exception) -> str:
    log_path = Path(tempfile.gettempdir()) / "DAMYComp_startup_error.log"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tb = traceback.format_exc()
    lines = [
        "=" * 72,
        f"[{ts}] Startup failure",
        f"Error: {err}",
        "",
        tb,
        "",
    ]
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return str(log_path)


def _write_unhandled_error_log(context: str, err: BaseException, tb_text: str) -> str:
    log_path = Path(tempfile.gettempdir()) / "DAMYComp_unhandled_error.log"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "=" * 72,
        f"[{ts}] {context}",
        f"Error: {err}",
        "",
        tb_text,
        "",
    ]
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return str(log_path)


def _install_global_exception_hooks() -> None:
    def _main_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            _append_ui_runtime_log("main-thread KeyboardInterrupt ignored by global hook")
            return
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log_path = _write_unhandled_error_log("Unhandled exception (main thread)", exc_value, tb_text)
        _append_ui_runtime_log(f"unhandled main-thread exception logged: {log_path}")
        print(tb_text, file=sys.stderr)
        print(f"[ERROR] Logged to: {log_path}", file=sys.stderr)

    def _thread_hook(args: threading.ExceptHookArgs):
        if args.exc_type and issubclass(args.exc_type, KeyboardInterrupt):
            _append_ui_runtime_log("thread KeyboardInterrupt ignored by global hook")
            return
        tb_text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        log_path = _write_unhandled_error_log(
            f"Unhandled exception (thread: {args.thread.name if args.thread else 'unknown'})",
            args.exc_value,
            tb_text,
        )
        _append_ui_runtime_log(f"unhandled thread exception logged: {log_path}")
        print(tb_text, file=sys.stderr)
        print(f"[ERROR] Logged to: {log_path}", file=sys.stderr)

    sys.excepthook = _main_hook
    threading.excepthook = _thread_hook


def _show_startup_error(err: Exception, log_path: str) -> None:
    detail = (
        "DAMY UI failed to start.\n\n"
        f"{err}\n\n"
        f"Error log:\n{log_path}"
    )
    app = QApplication.instance()
    created_here = False
    if app is None:
        app = QApplication(sys.argv)
        created_here = True
    show_critical_unified(None, "DAMY Startup Error", detail)
    if created_here:
        app.quit()

def run_app():
    try:
        _install_global_exception_hooks()
        _append_ui_runtime_log("run_app start")
        run_migrations(CONNINFO)
        ensure_base_directory(CONNINFO, BASE_DIRECTORY)

        app = QApplication(sys.argv)
        app.aboutToQuit.connect(lambda: _append_ui_runtime_log("QApplication aboutToQuit"))
        try:
            signal.signal(signal.SIGINT, lambda *_args: app.quit())
        except Exception:
            pass
        apply_dark_theme(app)

        window = DragDropFolders(
            db_host=DB_HOST,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            port=DB_PORT,
        )
        window.show()
        _append_ui_runtime_log("main window shown")

        try:
            exit_code = app.exec()
        except KeyboardInterrupt:
            _append_ui_runtime_log("KeyboardInterrupt during app.exec; exiting gracefully")
            exit_code = 0
        _append_ui_runtime_log(f"app.exec returned exit_code={exit_code}")
        sys.exit(exit_code)
    except KeyboardInterrupt:
        _append_ui_runtime_log("run_app interrupted by KeyboardInterrupt")
        raise SystemExit(0)
    except Exception as err:
        _append_ui_runtime_log(f"run_app exception: {err}")
        log_path = _write_startup_error_log(err)
        try:
            _show_startup_error(err, log_path)
        except Exception:
            print(f"[ERROR] DAMY startup failed: {err}", file=sys.stderr)
            print(f"[ERROR] Log: {log_path}", file=sys.stderr)
        raise SystemExit(1)
