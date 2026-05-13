import sys

from folder_manager.app import run_app


CALENDAR_IMPORT_FLAGS = {
    "--calendar-import",
    "--txt-only",
    "--run-cancellation",
    "--cancellation-source-dir",
    "--psd-dir",
    "--no-db-write",
    "--skip-cancellation",
    "--cancellation-report-json",
}

ORDER_IMPORT_FLAGS = {
    "--order-import",
    "--source-base-dir",
    "--test-label",
    "--imported-label",
    "--no-label-update",
    "--max-messages",
    "--label-window",
    "--cancel-token-path",
    "--only-pid",
    "--only-item-id",
    "--allow-non-damy-base",
    "--allow-non-test-base",
}


def run_calendar_import() -> None:
    from folder_manager.calendar_import_v3.main import main as run_calendar_import_main

    run_calendar_import_main()


def run_order_import() -> None:
    from folder_manager.order_import_v1.main import main as run_order_import_main

    run_order_import_main()


def _argv_has_any(flags: set[str]) -> bool:
    return any(arg in flags for arg in sys.argv[1:])


if __name__ == "__main__":
    if _argv_has_any(ORDER_IMPORT_FLAGS):
        run_order_import()
    elif _argv_has_any(CALENDAR_IMPORT_FLAGS):
        run_calendar_import()
    else:
        run_app()
