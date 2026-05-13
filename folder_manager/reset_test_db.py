from __future__ import annotations

import argparse
import os
import sys

import psycopg

from .config import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from .migrations import ensure_base_directory, run_migrations


DEFAULT_TEST_DB = "DAMY_TEST"
DEFAULT_TEST_BASE_DIR = r"T:\DAMY_TEST"


def _is_safe_test_db(dbname: str) -> bool:
    name = (dbname or "").strip().lower()
    if not name:
        return False
    if name in {"damy_workflow", "damy_workflow_v2"}:
        return False
    return "test" in name


def _conninfo(host: str, port: int, dbname: str, user: str, password: str) -> str:
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset TEST DB content (safe guard: only DB names containing 'test')."
    )
    parser.add_argument("--host", default=os.getenv("DAMY_DB_HOST", DB_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("DAMY_DB_PORT", str(DB_PORT))))
    parser.add_argument(
        "--db-name",
        default=os.getenv("DAMY_DB_NAME", DB_NAME or DEFAULT_TEST_DB),
        help="Target DB to reset. Must contain 'test' and cannot be a production DB.",
    )
    parser.add_argument("--user", default=os.getenv("DAMY_DB_USER", DB_USER))
    parser.add_argument("--password", default=os.getenv("DAMY_DB_PASS", DB_PASS))
    parser.add_argument(
        "--base-dir",
        default=os.getenv("DAMY_BASE_DIR", DEFAULT_TEST_BASE_DIR),
        help="Value to store in app_settings.base_directory after reset.",
    )
    parser.add_argument(
        "--clear-app-settings",
        action="store_true",
        help="Also clear app_settings before writing base_directory.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing.")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    db_name = args.db_name.strip()
    if not _is_safe_test_db(db_name):
        raise RuntimeError(
            "Refusing to reset DB. Target must be a TEST DB name containing 'test' "
            "and cannot be 'damy_workflow' or 'damy_workflow_v2'."
        )

    conninfo = _conninfo(args.host, args.port, db_name, args.user, args.password)

    run_migrations(conninfo)

    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM workflow_runs")
        before_count = int(cur.fetchone()[0])

        cur.execute("SELECT value FROM app_settings WHERE key='base_directory'")
        row = cur.fetchone()
        before_base = row[0] if row else None

    print(f"[INFO] Target DB: {db_name}")
    print(f"[INFO] workflow_runs before: {before_count}")
    print(f"[INFO] base_directory before: {before_base}")
    print(f"[INFO] base_directory after reset will be: {args.base_dir}")
    print(f"[INFO] clear_app_settings: {args.clear_app_settings}")

    if args.dry_run:
        print("[INFO] Dry-run mode: no write performed.")
        return

    if not args.yes:
        prompt = f"Type YES to reset DB '{db_name}': "
        confirmed = input(prompt).strip()
        if confirmed != "YES":
            print("[INFO] Cancelled.")
            return

    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE workflow_runs RESTART IDENTITY CASCADE")
        if args.clear_app_settings:
            cur.execute("DELETE FROM app_settings")
        conn.commit()

    ensure_base_directory(conninfo, args.base_dir)

    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM workflow_runs")
        after_count = int(cur.fetchone()[0])
        cur.execute("SELECT value FROM app_settings WHERE key='base_directory'")
        row = cur.fetchone()
        after_base = row[0] if row else None

    print(f"[OK] Reset complete for DB: {db_name}")
    print(f"[OK] workflow_runs after: {after_count}")
    print(f"[OK] base_directory after: {after_base}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
