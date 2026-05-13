from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import tkinter as tk
from tkinter import messagebox, ttk

import psycopg

from .config import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    ORDER_IMPORT_CREDENTIALS_PATH,
    ORDER_IMPORT_TOKEN_PATH,
)
from .db import DB
from .migrations import run_migrations
from .order_import_v1.main import (
    _append_or_create_order_pdf,
    _build_gmail_service,
    _count_order_packages_from_pdf,
    _parse_message_order,
    _render_order_to_pdf,
)


@dataclass(frozen=True)
class OrderEvent:
    event_id: int
    event_key: str
    message_id: str
    pid: str
    order_no: str
    is_late: bool
    pdf_output_path: str
    created_at: Any


@dataclass(frozen=True)
class JobChoice:
    item_id: int
    disk_name: str
    event_count: int
    normal_count: int
    late_count: int
    ignored_count: int
    normal_path: str
    late_path: str


def _conninfo(host: str, port: int, dbname: str, user: str, password: str) -> str:
    return f"host={host} port={port} dbname={dbname} user={user} password={password} connect_timeout=5"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Restore one DAMY job folder's order PDF(s) from Gmail using DB order_imported events. "
            "Default mode is dry-run; pass --apply to write files."
        )
    )
    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument("--folder", help="Exact DAMY workflow folder/disk_name to restore.")
    target.add_argument("--item-id", type=int, help="workflow_runs id to restore.")
    parser.add_argument("--kind", choices=("normal", "late", "both"), default="both")
    parser.add_argument("--apply", action="store_true", help="Actually recreate PDF files.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing target PDF. Without this, existing PDFs are skipped.",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Restore even if the current DB-linked PDF file exists. Implies a dry-run report unless --apply is set.",
    )
    parser.add_argument("--host", default=os.getenv("DAMY_DB_HOST", DB_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("DAMY_DB_PORT", str(DB_PORT))))
    parser.add_argument("--db-name", default=os.getenv("DAMY_DB_NAME", DB_NAME))
    parser.add_argument("--user", default=os.getenv("DAMY_DB_USER", DB_USER))
    parser.add_argument("--password", default=os.getenv("DAMY_DB_PASS", DB_PASS))
    parser.add_argument(
        "--gmail-token",
        default=os.getenv("DAMY_ORDER_IMPORT_TOKEN_PATH", ORDER_IMPORT_TOKEN_PATH),
        help="Gmail OAuth token path.",
    )
    parser.add_argument(
        "--gmail-credentials",
        default=os.getenv("DAMY_ORDER_IMPORT_CREDENTIALS_PATH", ORDER_IMPORT_CREDENTIALS_PATH),
        help="Gmail OAuth credentials path.",
    )
    return parser.parse_args(argv)


def _load_job_choices(conninfo: str) -> list[JobChoice]:
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH order_events AS (
                SELECT
                    wr.id AS item_id,
                    COUNT(*) FILTER (
                        WHERE NULLIF(BTRIM(COALESCE(we.payload->>'message_id', '')), '') IS NOT NULL
                          AND NULLIF(BTRIM(COALESCE(we.payload->>'pid', '')), '') IS NOT NULL
                          AND NULLIF(BTRIM(COALESCE(we.payload->>'order_no', '')), '') IS NOT NULL
                    )::int AS event_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE((we.payload->>'is_late')::boolean, false) = false
                          AND NULLIF(BTRIM(COALESCE(we.payload->>'message_id', '')), '') IS NOT NULL
                          AND NULLIF(BTRIM(COALESCE(we.payload->>'pid', '')), '') IS NOT NULL
                          AND NULLIF(BTRIM(COALESCE(we.payload->>'order_no', '')), '') IS NOT NULL
                    )::int AS normal_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE((we.payload->>'is_late')::boolean, false) = true
                          AND NULLIF(BTRIM(COALESCE(we.payload->>'message_id', '')), '') IS NOT NULL
                          AND NULLIF(BTRIM(COALESCE(we.payload->>'pid', '')), '') IS NOT NULL
                          AND NULLIF(BTRIM(COALESCE(we.payload->>'order_no', '')), '') IS NOT NULL
                    )::int AS late_count,
                    COUNT(*) FILTER (
                        WHERE NULLIF(BTRIM(COALESCE(we.payload->>'message_id', '')), '') IS NULL
                           OR NULLIF(BTRIM(COALESCE(we.payload->>'pid', '')), '') IS NULL
                           OR NULLIF(BTRIM(COALESCE(we.payload->>'order_no', '')), '') IS NULL
                    )::int AS ignored_count
                FROM workflow_events we
                JOIN workflow_runs wr ON wr.id = we.workflow_run_id
                WHERE we.event_type = 'order_imported'
                  AND wr.workflow_domain = 'prepaid'
                  AND wr.archived_at IS NULL
                GROUP BY wr.id
            ),
            asset_pivot AS (
                SELECT
                    workflow_run_id,
                    MAX(path) FILTER (WHERE asset_type = 'pdf' AND slot = 1) AS normal_path,
                    MAX(path) FILTER (WHERE asset_type = 'late_pdf' AND slot = 1) AS late_path
                FROM workflow_assets
                GROUP BY workflow_run_id
            )
            SELECT
                wr.id,
                wr.disk_name,
                oe.event_count,
                oe.normal_count,
                oe.late_count,
                oe.ignored_count,
                COALESCE(ap.normal_path, ''),
                COALESCE(ap.late_path, '')
            FROM order_events oe
            JOIN workflow_runs wr ON wr.id = oe.item_id
            LEFT JOIN asset_pivot ap ON ap.workflow_run_id = wr.id
            ORDER BY lower(wr.disk_name)
            """
        )
        rows = cur.fetchall()
    return [
        JobChoice(
            item_id=int(row[0]),
            disk_name=str(row[1] or ""),
            event_count=int(row[2] or 0),
            normal_count=int(row[3] or 0),
            late_count=int(row[4] or 0),
            ignored_count=int(row[5] or 0),
            normal_path=str(row[6] or ""),
            late_path=str(row[7] or ""),
        )
        for row in rows
    ]


def _path_status(path_value: str) -> str:
    value = str(path_value or "").strip()
    if not value:
        return "no path"
    try:
        return "exists" if Path(value).exists() else "missing"
    except Exception:
        return "unknown"


def _choose_job_with_dialog(choices: list[JobChoice]) -> tuple[JobChoice, str, bool, bool] | None:
    if not choices:
        messagebox.showerror("Restore Order PDF", "No jobs with order records were found in the database.")
        return None

    root = tk.Tk()
    root.title("Restore Order PDF From Gmail")
    root.geometry("1120x620")
    root.minsize(900, 480)

    selected: dict[str, Any] = {"value": None}
    search_var = tk.StringVar()
    kind_var = tk.StringVar(value="normal")
    apply_var = tk.BooleanVar(value=True)
    overwrite_var = tk.BooleanVar(value=False)

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    ttk.Label(top, text="Search folder / PID:").pack(side="left")
    search_entry = ttk.Entry(top, textvariable=search_var)
    search_entry.pack(side="left", fill="x", expand=True, padx=(8, 12))

    options = ttk.Frame(root, padding=(10, 0, 10, 8))
    options.pack(fill="x")
    ttk.Label(options, text="Restore:").pack(side="left")
    for label, value in (("Normal PDF", "normal"), ("Late PDF", "late"), ("Both", "both")):
        ttk.Radiobutton(options, text=label, value=value, variable=kind_var).pack(side="left", padx=(8, 0))
    ttk.Checkbutton(options, text="Actually restore files", variable=apply_var).pack(side="left", padx=(24, 0))
    ttk.Checkbutton(options, text="Overwrite existing target", variable=overwrite_var).pack(side="left", padx=(12, 0))

    columns = ("id", "folder", "orders", "normal", "late", "ignored", "normal_status", "late_status")
    tree = ttk.Treeview(root, columns=columns, show="headings", selectmode="browse")
    headings = {
        "id": "ID",
        "folder": "Folder",
        "orders": "Orders",
        "normal": "Normal",
        "late": "Late",
        "ignored": "Ignored",
        "normal_status": "Normal PDF",
        "late_status": "Late PDF",
    }
    widths = {
        "id": 70,
        "folder": 520,
        "orders": 70,
        "normal": 70,
        "late": 70,
        "ignored": 70,
        "normal_status": 120,
        "late_status": 120,
    }
    for col in columns:
        tree.heading(col, text=headings[col])
        tree.column(col, width=widths[col], anchor="w" if col == "folder" else "center")
    tree.pack(fill="both", expand=True, padx=10)

    scrollbar = ttk.Scrollbar(tree, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")

    footer = ttk.Frame(root, padding=10)
    footer.pack(fill="x")
    status_var = tk.StringVar()
    ttk.Label(footer, textvariable=status_var).pack(side="left", fill="x", expand=True)

    def visible_choices() -> list[JobChoice]:
        q = search_var.get().strip().lower()
        if not q:
            return choices
        return [
            choice
            for choice in choices
            if q in choice.disk_name.lower()
            or q in str(choice.item_id)
            or q in choice.normal_path.lower()
            or q in choice.late_path.lower()
        ]

    row_by_iid: dict[str, JobChoice] = {}

    def refresh() -> None:
        for iid in tree.get_children():
            tree.delete(iid)
        row_by_iid.clear()
        rows = visible_choices()
        for choice in rows:
            iid = str(choice.item_id)
            row_by_iid[iid] = choice
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    choice.item_id,
                    choice.disk_name,
                    choice.event_count,
                    choice.normal_count,
                    choice.late_count,
                    choice.ignored_count,
                    _path_status(choice.normal_path),
                    _path_status(choice.late_path),
                ),
            )
        status_var.set(f"{len(rows)} shown / {len(choices)} jobs with order records")
        if rows:
            tree.selection_set(str(rows[0].item_id))
            tree.focus(str(rows[0].item_id))

    def confirm_selection() -> None:
        selected_iids = tree.selection()
        if not selected_iids:
            messagebox.showwarning("Restore Order PDF", "Select one job first.")
            return
        choice = row_by_iid.get(selected_iids[0])
        if choice is None:
            return
        mode_text = "restore files" if apply_var.get() else "dry-run only"
        message = (
            f"Folder:\n{choice.disk_name}\n\n"
            f"Restorable orders: {choice.event_count}  Normal: {choice.normal_count}  "
            f"Late: {choice.late_count}  Ignored incomplete: {choice.ignored_count}\n"
            f"Action: {kind_var.get()} / {mode_text}\n"
            f"Overwrite existing: {'yes' if overwrite_var.get() else 'no'}"
        )
        if not messagebox.askokcancel("Confirm Restore", message):
            return
        selected["value"] = (choice, kind_var.get(), bool(apply_var.get()), bool(overwrite_var.get()))
        root.destroy()

    def cancel() -> None:
        selected["value"] = None
        root.destroy()

    ttk.Button(footer, text="Restore Selected", command=confirm_selection).pack(side="right")
    ttk.Button(footer, text="Cancel", command=cancel).pack(side="right", padx=(0, 8))
    search_var.trace_add("write", lambda *_args: refresh())
    search_entry.bind("<Return>", lambda _event: confirm_selection())
    tree.bind("<Double-1>", lambda _event: confirm_selection())
    root.protocol("WM_DELETE_WINDOW", cancel)

    refresh()
    search_entry.focus_set()
    root.mainloop()
    return selected["value"]


def _resolve_item(db: DB, *, item_id: int | None, folder: str | None):
    if item_id is not None:
        item = db.get_item_by_id(int(item_id))
    else:
        item = db.get_item_by_disk_name(str(folder or "").strip())
    if item is None:
        raise RuntimeError("Workflow item not found in DB.")
    return item


def _load_order_events(conninfo: str, item_id: int) -> list[OrderEvent]:
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                we.id,
                COALESCE(we.event_key, ''),
                COALESCE(we.payload->>'message_id', ''),
                COALESCE(we.payload->>'pid', ''),
                COALESCE(we.payload->>'order_no', ''),
                COALESCE((we.payload->>'is_late')::boolean, false),
                COALESCE(we.payload->>'pdf_output_path', ''),
                we.created_at
            FROM workflow_events we
            JOIN workflow_runs wr ON wr.id = we.workflow_run_id
            WHERE wr.id = %s
              AND wr.workflow_domain = 'prepaid'
              AND wr.archived_at IS NULL
              AND we.event_type = 'order_imported'
            ORDER BY we.created_at ASC, we.id ASC
            """,
            (item_id,),
        )
        rows = cur.fetchall()
    events: list[OrderEvent] = []
    for row in rows:
        message_id = str(row[2] or "").strip()
        pid = str(row[3] or "").strip().upper()
        order_no = str(row[4] or "").strip().upper()
        pdf_output_path = str(row[6] or "").strip()
        if not (message_id and pid and order_no):
            continue
        events.append(
            OrderEvent(
                event_id=int(row[0]),
                event_key=str(row[1] or ""),
                message_id=message_id,
                pid=pid,
                order_no=order_no,
                is_late=bool(row[5]),
                pdf_output_path=pdf_output_path,
                created_at=row[7],
            )
        )
    return events


def _event_kind(event: OrderEvent) -> str:
    return "late" if event.is_late else "normal"


def _target_path_for_kind(item, kind: str) -> Path | None:
    raw = getattr(item, "late_pdf_path", None) if kind == "late" else getattr(item, "pdf_path", None)
    value = str(raw or "").strip().strip('"')
    return Path(value) if value else None


def _filter_events_for_kind(events: Iterable[OrderEvent], kind: str) -> list[OrderEvent]:
    return [event for event in events if _event_kind(event) == kind]


def _restore_kind(
    *,
    db: DB,
    service,
    item_id: int,
    disk_name: str,
    kind: str,
    events: list[OrderEvent],
    target_path: Path,
    apply: bool,
    overwrite: bool,
    include_existing: bool,
) -> Path | None:
    if not events:
        print(f"[SKIP] {kind}: no order_imported events for {disk_name!r}")
        return None

    exists = target_path.exists()
    print(f"[INFO] {kind}: target={target_path}")
    print(f"[INFO] {kind}: events={len(events)} exists={exists}")
    if exists and not include_existing and not overwrite:
        print(f"[SKIP] {kind}: target exists. Use --include-existing to inspect or --overwrite --apply to rebuild.")
        return target_path
    if not apply:
        for event in events:
            print(
                f"[DRY] {kind}: would fetch message={event.message_id} "
                f"pid={event.pid} order={event.order_no} event_id={event.event_id}"
            )
        return target_path
    if exists and not overwrite:
        print(f"[SKIP] {kind}: target exists and --overwrite was not provided.")
        return target_path

    folder_path = target_path.parent
    folder_path.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    if target_path.exists():
        backup_path = target_path.with_suffix(target_path.suffix + ".restore_backup")
        target_path.replace(backup_path)
        print(f"[INFO] {kind}: existing target moved to {backup_path}")

    generated_path: Path | None = None
    try:
        for event in events:
            print(f"[GMAIL] {kind}: fetching {event.message_id} order={event.order_no}")
            msg = service.users().messages().get(userId="me", id=event.message_id, format="full").execute()
            parsed = _parse_message_order(msg)
            if parsed.order_no.upper() != event.order_no.upper():
                raise RuntimeError(
                    f"Message {event.message_id} parsed order {parsed.order_no}, "
                    f"but DB expected {event.order_no}."
                )
            effective_pid = event.pid or parsed.pid
            if not effective_pid:
                raise RuntimeError(f"Missing PID for message {event.message_id}.")

            with tempfile.NamedTemporaryFile(prefix="damy_restore_order_", suffix=".pdf", delete=False) as tmp:
                temp_pdf = Path(tmp.name)
            try:
                _render_order_to_pdf(parsed, temp_pdf)
                package_count = _count_order_packages_from_pdf(temp_pdf)
                generated_path = _append_or_create_order_pdf(
                    folder_path,
                    effective_pid,
                    parsed.order_date,
                    temp_pdf,
                    preferred_existing_pdf_path=str(generated_path or target_path),
                    new_order_package_count=package_count,
                    late=(kind == "late"),
                )
                print(f"[PDF] {kind}: added order={event.order_no} output={generated_path}")
            finally:
                try:
                    temp_pdf.unlink()
                except FileNotFoundError:
                    pass

        if generated_path is None:
            raise RuntimeError(f"{kind}: no PDF was generated.")
        if generated_path.resolve() != target_path.resolve():
            if target_path.exists():
                target_path.unlink()
            generated_path.replace(target_path)
            generated_path = target_path

        if kind == "late":
            db.set_late_pdf_path(item_id, str(target_path))
        else:
            db.set_pdf_path(item_id, str(target_path))

        if backup_path and backup_path.exists():
            backup_path.unlink()
        print(f"[OK] {kind}: restored {target_path}")
        return target_path
    except Exception:
        if generated_path and generated_path.exists() and generated_path.resolve() != target_path.resolve():
            try:
                generated_path.unlink()
            except Exception:
                pass
        if backup_path and backup_path.exists():
            if target_path.exists():
                target_path.unlink()
            backup_path.replace(target_path)
            print(f"[ROLLBACK] {kind}: restored original existing target from backup.")
        raise


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    conninfo = _conninfo(args.host, args.port, args.db_name, args.user, args.password)
    run_migrations(conninfo)
    db = DB(args.host, args.db_name, args.user, args.password, args.port)

    if args.item_id is None and not args.folder:
        choice_result = _choose_job_with_dialog(_load_job_choices(conninfo))
        if choice_result is None:
            print("[INFO] Cancelled.")
            return
        choice, selected_kind, selected_apply, selected_overwrite = choice_result
        args.item_id = choice.item_id
        args.kind = selected_kind
        args.apply = selected_apply
        args.overwrite = selected_overwrite

    item = _resolve_item(db, item_id=args.item_id, folder=args.folder)
    item_id = int(item.id)
    disk_name = str(item.disk_name)

    events = _load_order_events(db.conninfo, item_id)
    if not events:
        raise RuntimeError(f"No order_imported events found for {disk_name!r} (item_id={item_id}).")

    print(f"[INFO] item_id={item_id} folder={disk_name!r}")
    print(f"[INFO] mode={'APPLY' if args.apply else 'DRY-RUN'} kind={args.kind}")

    kinds = ["normal", "late"] if args.kind == "both" else [args.kind]
    service = None
    for kind in kinds:
        target_path = _target_path_for_kind(item, kind)
        kind_events = _filter_events_for_kind(events, kind)
        if target_path is None:
            if not kind_events:
                print(f"[SKIP] {kind}: no DB target path and no events.")
                continue
            latest_path = ""
            for event in reversed(kind_events):
                latest_path = str(event.pdf_output_path or "").strip().strip('"')
                if latest_path:
                    break
            if not latest_path:
                print(f"[SKIP] {kind}: no DB target path and no event has pdf_output_path.")
                continue
            target_path = Path(latest_path)
            print(f"[INFO] {kind}: using latest event pdf_output_path as target.")
        if args.apply and service is None:
            service = _build_gmail_service(Path(args.gmail_token), Path(args.gmail_credentials))
        _restore_kind(
            db=db,
            service=service,
            item_id=item_id,
            disk_name=disk_name,
            kind=kind,
            events=kind_events,
            target_path=target_path,
            apply=bool(args.apply),
            overwrite=bool(args.overwrite),
            include_existing=bool(args.include_existing),
        )

    if not args.apply:
        print("[INFO] Dry-run complete. Re-run with --apply to recreate missing PDF files.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
