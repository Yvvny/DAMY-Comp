# db.py
from __future__ import annotations

import hashlib
import json
import html
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, List, Any, Iterable

import psycopg


@dataclass(frozen=True)
class StageDefinition:
    stage: int
    label: str
    prefixes: tuple[str, ...]
    edit_column: bool = False


_DEFAULT_STAGE_CONFIG: List[StageDefinition] = [
    StageDefinition(stage=1, label="1. Upcoming", prefixes=("1. Upcoming",)),
    StageDefinition(stage=2, label="2. Select Best", prefixes=("2. Select Best",)),
    StageDefinition(stage=3, label="3. Edit", prefixes=("3. Edit",), edit_column=True),
    StageDefinition(stage=4, label="4. Print", prefixes=("4. Print",)),
    StageDefinition(stage=5, label="5. Package", prefixes=("5. Package",)),
    StageDefinition(stage=6, label="6. Confirm Delivery", prefixes=("6. Confirm Delivery",)),
    StageDefinition(stage=7, label="7. Finished", prefixes=("7. Finished",)),
]


def _coerce_prefixes(values: Optional[Iterable[str]], label: str) -> tuple[str, ...]:
    if not values:
        return (label,)
    prefixes = tuple(v for v in (val.strip() for val in values) if v)
    return prefixes or (label,)


def _load_stage_definitions() -> List[StageDefinition]:
    config_path = Path(__file__).with_name("stages.json")
    if not config_path.exists():
        return list(_DEFAULT_STAGE_CONFIG)

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("stages.json must contain a list")
    except Exception:
        # Fallback to defaults if the JSON is invalid
        return list(_DEFAULT_STAGE_CONFIG)

    stages: List[StageDefinition] = []
    seen_ids = set()
    try:
        for idx, entry in enumerate(raw, start=1):
            if isinstance(entry, str):
                stage_id = idx
                label = entry
                edit_column = False
                prefixes = (entry,)
            elif isinstance(entry, dict):
                stage_id = int(entry.get("stage") or entry.get("id") or idx)
                label = str(entry.get("label") or entry.get("name") or "").strip()
                if not label:
                    raise ValueError("Stage label missing in stages.json")
                edit_column = bool(entry.get("edit_column") or entry.get("edit"))
                prefixes = _coerce_prefixes(entry.get("prefixes"), label)
            else:
                raise ValueError("Invalid stage entry in stages.json")

            if stage_id in seen_ids:
                raise ValueError(f"Duplicate stage id {stage_id} in stages.json")
            seen_ids.add(stage_id)
            stages.append(StageDefinition(stage=stage_id, label=label, prefixes=prefixes, edit_column=edit_column))
    except Exception:
        return list(_DEFAULT_STAGE_CONFIG)

    return stages or list(_DEFAULT_STAGE_CONFIG)


STAGES: List[StageDefinition] = _load_stage_definitions()
DAYS = [stage.label for stage in STAGES]
_VALID_STAGE_IDS = {stage.stage for stage in STAGES}
WORKFLOW_DOMAIN_PREPAID = "prepaid"
WORKFLOW_DOMAIN_PROOFING = "proofing"
WORKFLOW_DOMAIN_YEARBOOK = "yearbook"
ORDER_IMPORT_SOURCE_GODADDY = "godaddy"
ORDER_IMPORT_SOURCE_PHOTODECK_PAID = "photodeck_paid"
_DETAIL_COLUMNS_BY_DOMAIN = {
    WORKFLOW_DOMAIN_PREPAID: {
        "pdf_path",
        "pdf_path_2",
        "pdf_path_3",
        "pdf_path_4",
        "late_pdf_path",
        "excel_path",
        "orders_form_path",
        "orders_form_path_2",
        "orders_form_path_3",
        "orders_form_path_4",
        "qr_roster_path",
        "qr_orders_path",
    },
    WORKFLOW_DOMAIN_PROOFING: {
        "pdf_path",
        "school_email_sent_at",
        "school_email_recipient",
    },
    WORKFLOW_DOMAIN_YEARBOOK: {
        "pdf_path",
        "school_email_sent_at",
        "school_email_recipient",
    },
}
_NORMALIZED_ASSET_TYPES = {
    "pdf",
    "late_pdf",
    "excel",
    "orders_form",
    "qr_roster",
    "qr_orders",
}
_ASSET_COLUMN_TO_TYPE_SLOT = {
    "pdf_path": ("pdf", 1),
    "pdf_path_2": ("pdf", 2),
    "pdf_path_3": ("pdf", 3),
    "pdf_path_4": ("pdf", 4),
    "late_pdf_path": ("late_pdf", 1),
    "excel_path": ("excel", 1),
    "orders_form_path": ("orders_form", 1),
    "orders_form_path_2": ("orders_form", 2),
    "orders_form_path_3": ("orders_form", 3),
    "orders_form_path_4": ("orders_form", 4),
    "qr_roster_path": ("qr_roster", 1),
    "qr_orders_path": ("qr_orders", 1),
}


# ---- Helpers: parse stage + clean display + parse shoot_date ----
_STAGE_PREFIX_RE = re.compile(r"^\s*(\d+)\.\s+")
_YYMMDD_RE = re.compile(r"^\s*(\d{6})\b")
_JOB_CODE_RE = re.compile(r"\bP\d{8,}\b")
_RICH_NOTE_PREFIX = "__DAMY_RICH_NOTE_HTML__:"
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}")


def detect_stage_from_disk_name(disk_name: str) -> int:
    # Prefer exact known prefixes
    s = disk_name.strip()
    for stage in STAGES:
        for prefix in stage.prefixes:
            if s.startswith(prefix):
                return stage.stage
    # Fallback: "N. " at start
    m = _STAGE_PREFIX_RE.match(s)
    if m:
        stage_id = int(m.group(1))
        if stage_id in _VALID_STAGE_IDS:
            return stage_id
    return STAGES[0].stage if STAGES else 1


def normalize_display_name(disk_name: str) -> str:
    """Remove leading 'N. Something' and old embedded tokens (I/G/ip(...)) for display purposes."""
    s = disk_name.strip()

    # Remove known stage prefix at the beginning
    removed_prefix = False
    for stage in STAGES:
        for prefix in stage.prefixes:
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
                removed_prefix = True
                break
        if removed_prefix:
            break

    # Remove generic "N. " prefix at the beginning
    s = _STAGE_PREFIX_RE.sub("", s).strip()

    # Remove old tokens if they exist in names
    s = re.sub(r"\bI\b", "", s)
    s = re.sub(r"\bG\b", "", s)
    s = re.sub(r"\bip\([^)]*\)", "", s)

    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def parse_shoot_date_from_display(display_name: str) -> Optional[date]:
    """
    Parses leading YYMMDD like '260109 ...' -> 2026-01-09.
    Returns None if not present / invalid.
    """
    m = _YYMMDD_RE.match(display_name.strip())
    if not m:
        return None
    yymmdd = m.group(1)
    yy = int(yymmdd[0:2])
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])
    yyyy = 2000 + yy
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def parse_job_code(display_name: str) -> Optional[str]:
    m = _JOB_CODE_RE.search(display_name)
    return m.group(0) if m else None


def note_payload_to_plain_text(note_payload: str | None) -> str:
    raw = str(note_payload or "")
    if not raw:
        return ""
    payload = raw[len(_RICH_NOTE_PREFIX):] if raw.startswith(_RICH_NOTE_PREFIX) else raw
    if "<" not in payload and ">" not in payload:
        return payload
    text = re.sub(r"(?i)<br\s*/?>", "\n", payload)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def parse_contact_fields_from_note(note_payload: str | None) -> tuple[Optional[str], Optional[str], Optional[str]]:
    plain = note_payload_to_plain_text(note_payload)
    if not plain:
        return None, None, None

    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    if not lines:
        return None, None, None

    first_email_match = _EMAIL_RE.search(plain)
    email_value = (first_email_match.group(0).strip() if first_email_match else "") or None

    first_phone_match = _PHONE_RE.search(plain)
    phone_value = (first_phone_match.group(0).strip() if first_phone_match else "") or None

    name_value = ""
    for idx, line in enumerate(lines):
        if re.search(r"\bbooked by\b", line, flags=re.IGNORECASE):
            if ":" in line:
                right = line.split(":", 1)[1].strip()
                if right and not re.search(r"\bbooked by\b", right, flags=re.IGNORECASE):
                    name_value = right
                    break
            if idx + 1 < len(lines):
                candidate = lines[idx + 1].strip()
                if candidate:
                    name_value = candidate
                    break

    if not name_value and len(lines) >= 2:
        name_value = lines[1].strip()

    if not name_value and lines:
        name_value = lines[0].strip()

    if name_value and email_value:
        name_value = _EMAIL_RE.sub("", name_value).strip(" ,;:-")

    name_value = re.sub(r"\s+", " ", name_value).strip()
    if not name_value:
        name_value = None

    return name_value, email_value, phone_value


# ---- Data model returned to UI ----
@dataclass(frozen=True)
class WorkflowItem:
    id: int
    disk_name: str
    display_name: str
    stage: int
    flag_i: bool
    flag_g: bool
    in_progress_by: Optional[str]
    pid: Optional[str]
    note: Optional[str]
    action_note: Optional[str]
    contact_name: Optional[str]
    contact_email: Optional[str]
    contact_phone: Optional[str]
    note_color: Optional[str]
    shoot_date: Optional[date]
    pdf_path: Optional[str]
    pdf_path_2: Optional[str]
    pdf_path_3: Optional[str]
    pdf_path_4: Optional[str]
    late_pdf_path: Optional[str]
    excel_path: Optional[str]
    orders_form_path: Optional[str]
    orders_form_path_2: Optional[str]
    orders_form_path_3: Optional[str]
    orders_form_path_4: Optional[str]
    qr_roster_path: Optional[str]
    qr_orders_path: Optional[str]
    workflow_domain: str = WORKFLOW_DOMAIN_PREPAID
    workflow_step: Optional[str] = None
    school_email_sent_at: Optional[Any] = None
    school_email_recipient: Optional[str] = None


class DB:
    """
    Psycopg3 helper for DAMY workflow DB.

    Expected tables:
      - workflow_runs(...)
      - app_settings(key,value)  with base_directory='T:\\DAMY' (optional but recommended)
    """

    def __init__(
        self,
        host: str,
        dbname: str,
        user: str,
        password: str,
        port: int = 5432,
        connect_timeout: int = 5,
    ):
        self.conninfo = (
            f"host={host} port={port} dbname={dbname} user={user} "
            f"password={password} connect_timeout={connect_timeout}"
        )

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo)

    @staticmethod
    def _is_missing_column_error(exc: Exception, column_name: str) -> bool:
        text = str(exc or "").lower()
        sqlstate = str(getattr(exc, "sqlstate", "") or "")
        return sqlstate == "42703" and str(column_name or "").lower() in text

    @staticmethod
    def _is_missing_action_note_error(exc: Exception) -> bool:
        return DB._is_missing_column_error(exc, "action_note")

    @staticmethod
    def _normalize_domain(domain: Optional[str]) -> str:
        return (domain or WORKFLOW_DOMAIN_PREPAID).strip().lower() or WORKFLOW_DOMAIN_PREPAID

    def has_workflow_domain_columns(self) -> bool:
        return self.has_normalized_workflow_view()

    def _prepaid_filter(self, initial_where: str, params: List[Any]) -> str:
        if not self.has_workflow_domain_columns():
            return initial_where
        params.extend([WORKFLOW_DOMAIN_PREPAID, WORKFLOW_DOMAIN_PREPAID])
        joiner = " AND " if initial_where.strip() else "WHERE "
        return (
            initial_where
            + joiner
            + "COALESCE(NULLIF(workflow_domain,''), %s) = %s"
        )

    def _workflow_domain_for_item(self, item_id: int) -> str:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(NULLIF(workflow_domain,''), %s)
                FROM workflow_runs
                WHERE id=%s
                """,
                (WORKFLOW_DOMAIN_PREPAID, item_id),
            )
            row = cur.fetchone()
        return self._normalize_domain(str(row[0] if row else WORKFLOW_DOMAIN_PREPAID))

    def _workflow_detail_column_allowed(self, item_id: int, column_name: str) -> bool:
        domain = self._workflow_domain_for_item(item_id)
        if not domain:
            return False
        return column_name in _DETAIL_COLUMNS_BY_DOMAIN.get(domain, set())

    def _set_workflow_detail_column(self, item_id: int, column_name: str, value: Optional[Any]) -> None:
        if not self._workflow_detail_column_allowed(item_id, column_name):
            return
        if column_name in _ASSET_COLUMN_TO_TYPE_SLOT:
            self._sync_workflow_asset_column(item_id, column_name, value)
            return
        if column_name in {"school_email_sent_at", "school_email_recipient"}:
            if column_name == "school_email_sent_at":
                self._update_workflow_run_fields(item_id, school_email_sent_at=value)
            else:
                self._update_workflow_run_fields(item_id, school_email_recipient=value)

    def set_workflow_detail_value(self, item_id: int, column_name: str, value: Optional[Any]) -> None:
        if not self._workflow_detail_column_allowed(item_id, column_name):
            return
        self._set_workflow_detail_column(item_id, column_name, value)

    def get_workflow_details(self, item_id: int) -> dict[str, Any]:
        item = self.get_item_by_id(item_id)
        if item is None:
            return {}
        details = {
            "pdf_path": item.pdf_path,
            "pdf_path_2": item.pdf_path_2,
            "pdf_path_3": item.pdf_path_3,
            "pdf_path_4": item.pdf_path_4,
            "late_pdf_path": item.late_pdf_path,
            "excel_path": item.excel_path,
            "orders_form_path": item.orders_form_path,
            "orders_form_path_2": item.orders_form_path_2,
            "orders_form_path_3": item.orders_form_path_3,
            "orders_form_path_4": item.orders_form_path_4,
            "qr_roster_path": item.qr_roster_path,
            "qr_orders_path": item.qr_orders_path,
        }
        if self._workflow_domain_for_item(item_id) in {WORKFLOW_DOMAIN_PROOFING, WORKFLOW_DOMAIN_YEARBOOK}:
            sent_at, recipient = self.get_school_email_status(item_id)
            details["school_email_sent_at"] = sent_at
            details["school_email_recipient"] = recipient or None
        return details

    def has_normalized_workflow_tables(self) -> bool:
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)::int
                    FROM unnest(ARRAY[
                        'jobs',
                        'workflow_runs',
                        'workflow_assets',
                        'workflow_events',
                        'parent_delivery_contacts',
                        'proofing_paid_order_assets'
                    ]) AS required_table(table_name)
                    WHERE to_regclass(required_table.table_name) IS NOT NULL
                    """
                )
                return int((cur.fetchone() or [0])[0] or 0) == 6
        except Exception:
            return False

    def has_normalized_workflow_view(self) -> bool:
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT to_regclass('workflow_items_normalized_view')")
                return cur.fetchone()[0] is not None
        except Exception:
            return False

    def _workflow_read_source(self) -> str:
        if not self.has_normalized_workflow_view():
            raise RuntimeError("workflow_items_normalized_view is missing. Please run database migrations.")
        return "workflow_items_normalized_view"

    def _order_import_event_key(self, source: str, pid: str, order_no: str) -> str:
        return f"order_import:{source}:{pid}:{order_no}"

    def _upsert_normalized_item_state(
        self,
        *,
        item_id: Optional[int] = None,
        disk_name: str,
        display_name: Optional[str] = None,
        stage: Optional[int] = None,
        domain: str = WORKFLOW_DOMAIN_PREPAID,
        step: Optional[str] = None,
        pid: Optional[str] = None,
        shoot_date: Optional[date] = None,
        sort_key: int = 0,
        flag_i: bool = False,
        flag_g: bool = False,
        in_progress_by: Optional[str] = None,
        note: Optional[str] = None,
        action_note: Optional[str] = None,
        contact_name: Optional[str] = None,
        contact_email: Optional[str] = None,
        contact_phone: Optional[str] = None,
        note_color: Optional[str] = None,
        school_email_sent_at: Optional[Any] = None,
        school_email_recipient: Optional[str] = None,
    ) -> int:
        raw_disk_name = (disk_name or "").strip()
        if not raw_disk_name:
            raise ValueError("disk_name cannot be empty")

        display_value = (display_name or normalize_display_name(raw_disk_name)).strip() or raw_disk_name
        domain_value = self._normalize_domain(domain)
        step_value = (step or "").strip() or None
        pid_value = (str(pid or "").strip() or parse_job_code(display_value) or None)
        shoot_date_value = shoot_date or parse_shoot_date_from_display(display_value)
        stage_value = int(stage if stage is not None else detect_stage_from_disk_name(raw_disk_name))
        canonical_name = display_value or raw_disk_name

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_runs(
                    workflow_domain, workflow_step, disk_name, display_name,
                    stage, sort_key, flag_i, flag_g, in_progress_by,
                    note, action_note, note_color, school_email_sent_at,
                    school_email_recipient, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (workflow_domain, disk_name) DO UPDATE SET
                    workflow_step = COALESCE(EXCLUDED.workflow_step, workflow_runs.workflow_step),
                    display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), workflow_runs.display_name),
                    stage = EXCLUDED.stage,
                    sort_key = CASE
                        WHEN EXCLUDED.sort_key = 0 THEN COALESCE(workflow_runs.sort_key, 0)
                        ELSE EXCLUDED.sort_key
                    END,
                    flag_i = COALESCE(workflow_runs.flag_i, false) OR EXCLUDED.flag_i,
                    flag_g = COALESCE(workflow_runs.flag_g, false) OR EXCLUDED.flag_g,
                    in_progress_by = COALESCE(EXCLUDED.in_progress_by, workflow_runs.in_progress_by),
                    note = COALESCE(EXCLUDED.note, workflow_runs.note),
                    action_note = COALESCE(EXCLUDED.action_note, workflow_runs.action_note),
                    note_color = COALESCE(EXCLUDED.note_color, workflow_runs.note_color),
                    school_email_sent_at = COALESCE(EXCLUDED.school_email_sent_at, workflow_runs.school_email_sent_at),
                    school_email_recipient = COALESCE(EXCLUDED.school_email_recipient, workflow_runs.school_email_recipient),
                    archived_at = NULL,
                    updated_at = now()
                RETURNING id
                """,
                (
                    domain_value,
                    step_value,
                    raw_disk_name,
                    display_value,
                    stage_value,
                    int(sort_key),
                    bool(flag_i),
                    bool(flag_g),
                    in_progress_by,
                    note,
                    action_note,
                    note_color,
                    school_email_sent_at,
                    school_email_recipient,
                ),
            )
            run_row = cur.fetchone()
            if not run_row:
                raise RuntimeError(f"Failed to upsert workflow run for {domain_value}:{raw_disk_name}")
            run_id = int(run_row[0])

            cur.execute(
                """
                INSERT INTO jobs(
                    workflow_run_id, pid, canonical_name, display_name,
                    shoot_date, contact_name, contact_email, contact_phone, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (workflow_run_id) DO UPDATE SET
                    pid = COALESCE(EXCLUDED.pid, jobs.pid),
                    canonical_name = COALESCE(NULLIF(EXCLUDED.canonical_name, ''), jobs.canonical_name),
                    display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), jobs.display_name),
                    shoot_date = COALESCE(EXCLUDED.shoot_date, jobs.shoot_date),
                    contact_name = COALESCE(NULLIF(BTRIM(EXCLUDED.contact_name), ''), jobs.contact_name),
                    contact_email = COALESCE(NULLIF(BTRIM(EXCLUDED.contact_email), ''), jobs.contact_email),
                    contact_phone = COALESCE(NULLIF(BTRIM(EXCLUDED.contact_phone), ''), jobs.contact_phone),
                    updated_at = now()
                """,
                (
                    run_id,
                    pid_value,
                    canonical_name,
                    display_value,
                    shoot_date_value,
                    contact_name,
                    contact_email,
                    contact_phone,
                ),
            )
        return run_id

    def _update_workflow_run_fields(self, item_id: int, **fields: Any) -> None:
        run_id = self._ensure_normalized_workflow_run(item_id)
        if run_id is None or not fields:
            return
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            assignments.append(f"{key}=%s")
            values.append(value)
        values.append(run_id)
        sql = f"UPDATE workflow_runs SET {', '.join(assignments)}, updated_at=now() WHERE id=%s"
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(values))

    def _update_job_fields(self, item_id: int, **fields: Any) -> None:
        run_id = self._ensure_normalized_workflow_run(item_id)
        if run_id is None or not fields:
            return
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM jobs
                WHERE workflow_run_id=%s
                """,
                (run_id,),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return
            job_id = int(row[0])
            assignments: list[str] = []
            values: list[Any] = []
            for key, value in fields.items():
                assignments.append(f"{key}=%s")
                values.append(value)
            values.append(job_id)
            sql = f"UPDATE jobs SET {', '.join(assignments)}, updated_at=now() WHERE id=%s"
            cur.execute(sql, tuple(values))

    def _ensure_normalized_workflow_run(self, item_id: int) -> Optional[int]:
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id
                    FROM workflow_runs
                    WHERE id=%s
                      AND archived_at IS NULL
                    """,
                    (item_id,),
                )
                row = cur.fetchone()
                if row:
                    return int(row[0])
        except Exception as exc:
            sqlstate = str(getattr(exc, "sqlstate", "") or "")
            if sqlstate in {"42P01", "42703", "42P10"}:
                return None
            raise
        return None

    def get_normalized_workflow_run_id(self, item_id: int) -> Optional[int]:
        return self._ensure_normalized_workflow_run(item_id)

    def set_workflow_asset(
        self,
        item_id: int,
        asset_type: str,
        path: Optional[str],
        *,
        slot: int = 1,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        asset_type = str(asset_type or "").strip()
        if asset_type not in _NORMALIZED_ASSET_TYPES or int(slot) < 1:
            return
        run_id = self._ensure_normalized_workflow_run(item_id)
        if run_id is None:
            return
        path_value = str(path or "").strip()
        metadata_value = json.dumps(metadata or {})
        try:
            with self.connect() as conn, conn.cursor() as cur:
                if not path_value:
                    cur.execute(
                        """
                        DELETE FROM workflow_assets
                        WHERE workflow_run_id=%s AND asset_type=%s AND slot=%s
                        """,
                        (run_id, asset_type, int(slot)),
                    )
                    return
                cur.execute(
                    """
                    INSERT INTO workflow_assets(workflow_run_id, asset_type, slot, path, metadata)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (workflow_run_id, asset_type, slot) DO UPDATE SET
                        path = EXCLUDED.path,
                        metadata = workflow_assets.metadata || EXCLUDED.metadata,
                        updated_at = now()
                    """,
                    (run_id, asset_type, int(slot), path_value, metadata_value),
                )
        except Exception as exc:
            sqlstate = str(getattr(exc, "sqlstate", "") or "")
            if sqlstate in {"42P01", "42703", "42P10"}:
                return
            raise

    def _sync_workflow_asset_column(self, item_id: int, column_name: str, value: Optional[Any]) -> None:
        spec = _ASSET_COLUMN_TO_TYPE_SLOT.get(column_name)
        if not spec:
            return
        asset_type, slot = spec
        self.set_workflow_asset(
            item_id,
            asset_type,
            None if value is None else str(value),
            slot=slot,
            metadata={"source": "legacy_column", "legacy_column": column_name},
        )

    def list_workflow_assets(self, item_id: int) -> list[dict[str, Any]]:
        run_id = self._ensure_normalized_workflow_run(item_id)
        if run_id is None:
            return []
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT asset_type, slot, path, metadata, updated_at
                    FROM workflow_assets
                    WHERE workflow_run_id=%s
                    ORDER BY asset_type, slot
                    """,
                    (run_id,),
                )
                rows = cur.fetchall()
        except Exception as exc:
            sqlstate = str(getattr(exc, "sqlstate", "") or "")
            if sqlstate in {"42P01", "42703"}:
                return []
            raise
        return [
            {
                "asset_type": row[0],
                "slot": row[1],
                "path": row[2],
                "metadata": row[3] or {},
                "updated_at": row[4],
            }
            for row in rows
        ]

    def record_workflow_event(
        self,
        item_id: int,
        event_type: str,
        *,
        source: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        event_key: Optional[str] = None,
    ) -> None:
        event_type = str(event_type or "").strip()
        if not event_type:
            return
        run_id = self._ensure_normalized_workflow_run(item_id)
        if run_id is None:
            return
        event_key_value = str(event_key or "").strip()
        if not event_key_value:
            payload_fingerprint = json.dumps(payload or {}, sort_keys=True, default=str)
            digest = hashlib.sha256(payload_fingerprint.encode("utf-8")).hexdigest()
            event_key_value = f"{event_type}:{source or ''}:{run_id}:{digest}"
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO workflow_events(workflow_run_id, event_type, source, event_key, payload)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (event_key) DO UPDATE SET
                        workflow_run_id = EXCLUDED.workflow_run_id,
                        source = EXCLUDED.source,
                        payload = EXCLUDED.payload
                    """,
                    (
                        run_id,
                        event_type,
                        source,
                        event_key_value,
                        json.dumps(payload or {}),
                    ),
                )
        except Exception as exc:
            sqlstate = str(getattr(exc, "sqlstate", "") or "")
            if sqlstate in {"42P01", "42703", "42P10"}:
                return
            raise

    def check_database_health(self) -> list[str]:
        """
        Return database consistency warnings for the normalized workflow schema.
        Empty list means no known schema/data issue was found.
        """
        issues: list[str] = []
        required_tables = [
            "jobs",
            "workflow_runs",
            "workflow_assets",
            "workflow_events",
            "parent_delivery_contacts",
            "proofing_paid_order_assets",
            "workflow_items_normalized_view",
        ]

        def add_count_issue(cur, sql: str, params: tuple[Any, ...], message: str) -> None:
            cur.execute(sql, params)
            count = int((cur.fetchone() or [0])[0] or 0)
            if count:
                issues.append(message.format(count=count))

        try:
            with self.connect() as conn, conn.cursor() as cur:
                missing_tables: list[str] = []
                for table_name in required_tables:
                    cur.execute("SELECT to_regclass(%s)", (table_name,))
                    if cur.fetchone()[0] is None:
                        missing_tables.append(table_name)
                if missing_tables:
                    issues.append(f"Missing database tables: {', '.join(missing_tables)}")
                    return issues

                add_count_issue(
                    cur,
                    """
                    SELECT COUNT(*)::int
                    FROM workflow_runs
                    WHERE COALESCE(NULLIF(workflow_domain,''), 'prepaid')
                      NOT IN ('prepaid', 'proofing', 'yearbook')
                    """,
                    (),
                    "{count} workflow_runs rows have an invalid workflow_domain.",
                )

                add_count_issue(
                    cur,
                    """
                    SELECT COUNT(*)::int
                    FROM workflow_runs wr
                    LEFT JOIN jobs j ON j.workflow_run_id = wr.id
                    WHERE j.id IS NULL
                    """,
                    (),
                    "{count} workflow_runs rows are not linked to jobs.",
                )

                add_count_issue(
                    cur,
                    """
                    SELECT COUNT(*)::int
                    FROM (
                        SELECT workflow_domain, disk_name
                        FROM workflow_runs
                        WHERE archived_at IS NULL
                        GROUP BY workflow_domain, disk_name
                        HAVING COUNT(*) > 1
                    ) duplicates
                    """,
                    (),
                    "{count} workflow domain/disk_name combinations are duplicated.",
                )

                add_count_issue(
                    cur,
                    """
                    SELECT COUNT(*)::int
                    FROM workflow_assets
                    WHERE NULLIF(BTRIM(path), '') IS NULL
                    """,
                    (),
                    "{count} workflow_assets rows have empty paths.",
                )

                add_count_issue(
                    cur,
                    """
                    SELECT COUNT(*)::int
                    FROM jobs j
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM workflow_runs wr
                        WHERE j.workflow_run_id = wr.id
                    )
                    """,
                    (),
                    "{count} jobs rows are orphaned and not linked to workflow_runs.",
                )

                add_count_issue(
                    cur,
                    """
                    SELECT COUNT(*)::int
                    FROM workflow_runs wr
                    WHERE wr.workflow_domain IN ('proofing', 'yearbook')
                      AND wr.school_email_sent_at IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1
                        FROM workflow_events we
                        WHERE we.workflow_run_id = wr.id
                          AND we.event_type = 'school_email_sent'
                      )
                    """,
                    (),
                    "{count} proofing/yearbook runs have school_email status but no workflow_events history.",
                )

                for legacy_table in (
                    "workflow_items",
                    "prepaid_workflow_details",
                    "proofing_workflow_details",
                    "yearbook_workflow_details",
                    "order_import_history",
                ):
                    cur.execute("SELECT to_regclass(%s)", (legacy_table,))
                    if cur.fetchone()[0] is not None:
                        issues.append(f"Legacy table still exists and should be removed: {legacy_table}")

                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'workflow_events'
                      AND column_name = 'legacy_order_import_history_id'
                    LIMIT 1
                    """
                )
                if cur.fetchone() is not None:
                    issues.append("workflow_events.legacy_order_import_history_id still exists and should be removed.")
        except Exception as exc:
            issues.append(f"Database health check failed: {exc}")

        return issues

    @staticmethod
    def _is_missing_contact_columns_error(exc: Exception) -> bool:
        return (
            DB._is_missing_column_error(exc, "contact_name")
            or DB._is_missing_column_error(exc, "contact_email")
            or DB._is_missing_column_error(exc, "contact_phone")
        )

    @staticmethod
    def _row_to_item_with_contact(r) -> WorkflowItem:
        row_len = len(r)
        return WorkflowItem(
            id=r[0],
            disk_name=r[1],
            display_name=r[2],
            stage=r[3],
            flag_i=r[4],
            flag_g=r[5],
            in_progress_by=r[6],
            pid=r[7],
            note=r[8],
            action_note=r[9],
            contact_name=r[10],
            contact_email=r[11],
            contact_phone=r[12],
            note_color=r[13],
            shoot_date=r[14],
            pdf_path=r[15],
            pdf_path_2=r[16],
            pdf_path_3=r[17],
            pdf_path_4=r[18],
            late_pdf_path=r[19],
            excel_path=r[20],
            orders_form_path=r[21],
            orders_form_path_2=r[22],
            orders_form_path_3=r[23],
            orders_form_path_4=r[24],
            qr_roster_path=r[25],
            qr_orders_path=r[26],
            workflow_domain=r[27] if row_len > 27 and r[27] else WORKFLOW_DOMAIN_PREPAID,
            workflow_step=r[28] if row_len > 28 else None,
            school_email_sent_at=r[29] if row_len > 29 else None,
            school_email_recipient=r[30] if row_len > 30 else None,
        )

    @staticmethod
    def _row_to_item_with_action_note(r) -> WorkflowItem:
        return WorkflowItem(
            id=r[0],
            disk_name=r[1],
            display_name=r[2],
            stage=r[3],
            flag_i=r[4],
            flag_g=r[5],
            in_progress_by=r[6],
            pid=r[7],
            note=r[8],
            action_note=r[9],
            contact_name=None,
            contact_email=None,
            contact_phone=None,
            note_color=r[10],
            shoot_date=r[11],
            pdf_path=r[12],
            pdf_path_2=r[13],
            pdf_path_3=r[14],
            pdf_path_4=r[15],
            late_pdf_path=r[16],
            excel_path=r[17],
            orders_form_path=r[18],
            orders_form_path_2=r[19],
            orders_form_path_3=r[20],
            orders_form_path_4=r[21],
            qr_roster_path=r[22],
            qr_orders_path=r[23],
        )

    @staticmethod
    def _row_to_item_legacy(r) -> WorkflowItem:
        return WorkflowItem(
            id=r[0],
            disk_name=r[1],
            display_name=r[2],
            stage=r[3],
            flag_i=r[4],
            flag_g=r[5],
            in_progress_by=r[6],
            pid=r[7],
            note=r[8],
            action_note=None,
            contact_name=None,
            contact_email=None,
            contact_phone=None,
            note_color=r[9],
            shoot_date=r[10],
            pdf_path=r[11],
            pdf_path_2=r[12],
            pdf_path_3=r[13],
            pdf_path_4=r[14],
            late_pdf_path=r[15],
            excel_path=r[16],
            orders_form_path=r[17],
            orders_form_path_2=r[18],
            orders_form_path_3=r[19],
            orders_form_path_4=r[20],
            qr_roster_path=r[21],
            qr_orders_path=r[22],
        )

    # ---------------- Settings ----------------
    def get_base_dir(self, fallback: Optional[str] = None) -> str:
        """
        Reads app_settings.base_directory. If missing, returns fallback or raises.
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key='base_directory'")
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
        if fallback:
            return fallback
        raise RuntimeError("app_settings.base_directory not set")

    def set_base_dir(self, base_dir: str) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings(key, value)
                VALUES ('base_directory', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (base_dir,),
            )

    # ---------------- Reads ----------------
    def list_by_stage(self, stage: int, *, domain: str = WORKFLOW_DOMAIN_PREPAID) -> List[WorkflowItem]:
        """
        Returns items in a stage sorted by shoot_date then display_name.
        """
        domain_norm = self._normalize_domain(domain)
        if domain_norm != WORKFLOW_DOMAIN_PREPAID and not self.has_workflow_domain_columns():
            return []
        source = self._workflow_read_source()
        where = "WHERE stage = %s"
        params: List[Any] = [stage]
        if self.has_workflow_domain_columns():
            where += " AND COALESCE(NULLIF(workflow_domain,''), %s) = %s"
            params.extend([WORKFLOW_DOMAIN_PREPAID, domain_norm])
        sql = f"""
            SELECT
              id,
              disk_name,
              COALESCE(NULLIF(display_name,''), disk_name) AS display_name,
              stage,
              flag_i,
              flag_g,
              in_progress_by,
              pid,
              note,
              action_note,
              contact_name,
              contact_email,
              contact_phone,
              note_color,
              shoot_date,
              pdf_path,
              pdf_path_2,
              pdf_path_3,
              pdf_path_4,
              late_pdf_path,
              excel_path,
              orders_form_path,
              orders_form_path_2,
              orders_form_path_3,
              orders_form_path_4,
              qr_roster_path,
              qr_orders_path,
              workflow_domain,
              workflow_step,
              school_email_sent_at,
              school_email_recipient
            FROM {source}
            {where}
            ORDER BY
              shoot_date NULLS LAST,
              display_name
        """
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
            return [self._row_to_item_with_contact(r) for r in rows]
        except Exception as exc:
            if self._is_missing_contact_columns_error(exc):
                action_params = list(params)
                action_only_sql = f"""
                    SELECT
                      id,
                      disk_name,
                      COALESCE(NULLIF(display_name,''), disk_name) AS display_name,
                      stage,
                      flag_i,
                      flag_g,
                      in_progress_by,
                      pid,
                      note,
                      action_note,
                      note_color,
                      shoot_date,
                      pdf_path,
                      pdf_path_2,
                      pdf_path_3,
                      pdf_path_4,
                      late_pdf_path,
                      excel_path,
                      orders_form_path,
                      orders_form_path_2,
                      orders_form_path_3,
                      orders_form_path_4,
                      qr_roster_path,
                      qr_orders_path
                    FROM {source}
                    {where}
                    ORDER BY
                      shoot_date NULLS LAST,
                      display_name
                """
                with self.connect() as conn, conn.cursor() as cur:
                    cur.execute(action_only_sql, tuple(action_params))
                    rows = cur.fetchall()
                return [self._row_to_item_with_action_note(r) for r in rows]
            if not self._is_missing_action_note_error(exc):
                raise

        # Backward compatibility: DB not yet migrated with action_note.
        legacy_params = list(params)
        legacy_sql = f"""
            SELECT
              id,
              disk_name,
              COALESCE(NULLIF(display_name,''), disk_name) AS display_name,
              stage,
              flag_i,
              flag_g,
              in_progress_by,
              pid,
              note,
              note_color,
              shoot_date,
              pdf_path,
              pdf_path_2,
              pdf_path_3,
              pdf_path_4,
              late_pdf_path,
              excel_path,
              orders_form_path,
              orders_form_path_2,
              orders_form_path_3,
              orders_form_path_4,
              qr_roster_path,
              qr_orders_path,
              workflow_domain,
              workflow_step,
              school_email_sent_at,
              school_email_recipient
            FROM {source}
            {where}
            ORDER BY
              shoot_date NULLS LAST,
              display_name
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(legacy_sql, tuple(legacy_params))
            rows = cur.fetchall()
        return [self._row_to_item_legacy(r) for r in rows]

    def list_all(self, *, domain: str = WORKFLOW_DOMAIN_PREPAID) -> List[WorkflowItem]:
        """
        Returns all workflow rows sorted by stage, then shoot_date, then display_name.
        Useful for board initialization to avoid one query per stage.
        """
        domain_norm = self._normalize_domain(domain)
        if domain_norm != WORKFLOW_DOMAIN_PREPAID and not self.has_workflow_domain_columns():
            return []
        source = self._workflow_read_source()
        where = ""
        params: List[Any] = []
        if self.has_workflow_domain_columns():
            where = "WHERE COALESCE(NULLIF(workflow_domain,''), %s) = %s"
            params.extend([WORKFLOW_DOMAIN_PREPAID, domain_norm])
        sql = f"""
            SELECT
              id,
              disk_name,
              COALESCE(NULLIF(display_name,''), disk_name) AS display_name,
              stage,
              flag_i,
              flag_g,
              in_progress_by,
              pid,
              note,
              action_note,
              contact_name,
              contact_email,
              contact_phone,
              note_color,
              shoot_date,
              pdf_path,
              pdf_path_2,
              pdf_path_3,
              pdf_path_4,
              late_pdf_path,
              excel_path,
              orders_form_path,
              orders_form_path_2,
              orders_form_path_3,
              orders_form_path_4,
              qr_roster_path,
              qr_orders_path,
              workflow_domain,
              workflow_step,
              school_email_sent_at,
              school_email_recipient
            FROM {source}
            {where}
            ORDER BY
              stage,
              shoot_date NULLS LAST,
              display_name
        """
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
            return [self._row_to_item_with_contact(r) for r in rows]
        except Exception as exc:
            if self._is_missing_contact_columns_error(exc):
                action_only_sql = f"""
                    SELECT
                      id,
                      disk_name,
                      COALESCE(NULLIF(display_name,''), disk_name) AS display_name,
                      stage,
                      flag_i,
                      flag_g,
                      in_progress_by,
                      pid,
                      note,
                      action_note,
                      note_color,
                      shoot_date,
                      pdf_path,
                      pdf_path_2,
                      pdf_path_3,
                      pdf_path_4,
                      late_pdf_path,
                      excel_path,
                      orders_form_path,
                      orders_form_path_2,
                      orders_form_path_3,
                      orders_form_path_4,
                      qr_roster_path,
                      qr_orders_path
                    FROM {source}
                    {where}
                    ORDER BY
                      stage,
                      shoot_date NULLS LAST,
                      display_name
                """
                with self.connect() as conn, conn.cursor() as cur:
                    cur.execute(action_only_sql, tuple(params))
                    rows = cur.fetchall()
                return [self._row_to_item_with_action_note(r) for r in rows]
            if not self._is_missing_action_note_error(exc):
                raise

        legacy_sql = f"""
            SELECT
              id,
              disk_name,
              COALESCE(NULLIF(display_name,''), disk_name) AS display_name,
              stage,
              flag_i,
              flag_g,
              in_progress_by,
              pid,
              note,
              note_color,
              shoot_date,
              pdf_path,
              pdf_path_2,
              pdf_path_3,
              pdf_path_4,
              late_pdf_path,
              excel_path,
              orders_form_path,
              orders_form_path_2,
              orders_form_path_3,
              orders_form_path_4,
              qr_roster_path,
              qr_orders_path
            FROM {source}
            {where}
            ORDER BY
              stage,
              shoot_date NULLS LAST,
              display_name
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(legacy_sql, tuple(params))
            rows = cur.fetchall()
        return [self._row_to_item_legacy(r) for r in rows]

    def get_item_by_id(self, item_id: int) -> Optional[WorkflowItem]:
        source = self._workflow_read_source()
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      id, disk_name, COALESCE(NULLIF(display_name,''), disk_name),
                      stage, flag_i, flag_g, in_progress_by, pid, note, action_note, contact_name, contact_email, contact_phone, note_color, shoot_date, pdf_path, pdf_path_2,
                      pdf_path_3, pdf_path_4, late_pdf_path, excel_path, orders_form_path, orders_form_path_2, orders_form_path_3, orders_form_path_4,
                      qr_roster_path, qr_orders_path, workflow_domain, workflow_step,
                      school_email_sent_at, school_email_recipient
                    FROM {source}
                    WHERE id=%s
                    """,
                    (item_id,),
                )
                r = cur.fetchone()
            return self._row_to_item_with_contact(r) if r else None
        except Exception as exc:
            if self._is_missing_contact_columns_error(exc):
                with self.connect() as conn, conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT
                          id, disk_name, COALESCE(NULLIF(display_name,''), disk_name),
                          stage, flag_i, flag_g, in_progress_by, pid, note, action_note, note_color, shoot_date, pdf_path, pdf_path_2,
                          pdf_path_3, pdf_path_4, late_pdf_path, excel_path, orders_form_path, orders_form_path_2, orders_form_path_3, orders_form_path_4,
                      qr_roster_path, qr_orders_path, workflow_domain, workflow_step,
                      school_email_sent_at, school_email_recipient
                        FROM {source}
                        WHERE id=%s
                        """,
                        (item_id,),
                    )
                    r = cur.fetchone()
                return self._row_to_item_with_action_note(r) if r else None
            if not self._is_missing_action_note_error(exc):
                raise

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  id, disk_name, COALESCE(NULLIF(display_name,''), disk_name),
                  stage, flag_i, flag_g, in_progress_by, pid, note, note_color, shoot_date, pdf_path, pdf_path_2,
                  pdf_path_3, pdf_path_4, late_pdf_path, excel_path, orders_form_path, orders_form_path_2, orders_form_path_3, orders_form_path_4,
                  qr_roster_path, qr_orders_path
                FROM {source}
                WHERE id=%s
                """,
                (item_id,),
            )
            r = cur.fetchone()
        return self._row_to_item_legacy(r) if r else None

    def get_item_by_disk_name(
        self,
        disk_name: str,
        *,
        domain: str = WORKFLOW_DOMAIN_PREPAID,
        stage: Optional[int] = None,
    ) -> Optional[WorkflowItem]:
        domain_norm = self._normalize_domain(domain)
        if domain_norm != WORKFLOW_DOMAIN_PREPAID and not self.has_workflow_domain_columns():
            return None
        source = self._workflow_read_source()
        where_parts = ["disk_name=%s"]
        params: List[Any] = [disk_name]
        if self.has_workflow_domain_columns():
            where_parts.append("COALESCE(NULLIF(workflow_domain,''), %s) = %s")
            params.extend([WORKFLOW_DOMAIN_PREPAID, domain_norm])
        if stage is not None:
            where_parts.append("stage=%s")
            params.append(int(stage))
        where = " AND ".join(where_parts)
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      id, disk_name, COALESCE(NULLIF(display_name,''), disk_name),
                      stage, flag_i, flag_g, in_progress_by, pid, note, action_note, contact_name, contact_email, contact_phone, note_color, shoot_date, pdf_path, pdf_path_2,
                      pdf_path_3, pdf_path_4, late_pdf_path, excel_path, orders_form_path, orders_form_path_2, orders_form_path_3, orders_form_path_4,
                      qr_roster_path, qr_orders_path, workflow_domain, workflow_step,
                      school_email_sent_at, school_email_recipient
                    FROM {source}
                    WHERE {where}
                    """,
                    tuple(params),
                )
                r = cur.fetchone()
            return self._row_to_item_with_contact(r) if r else None
        except Exception as exc:
            if self._is_missing_contact_columns_error(exc):
                with self.connect() as conn, conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT
                          id, disk_name, COALESCE(NULLIF(display_name,''), disk_name),
                          stage, flag_i, flag_g, in_progress_by, pid, note, action_note, note_color, shoot_date, pdf_path, pdf_path_2,
                          pdf_path_3, pdf_path_4, late_pdf_path, excel_path, orders_form_path, orders_form_path_2, orders_form_path_3, orders_form_path_4,
                          qr_roster_path, qr_orders_path
                        FROM {source}
                        WHERE {where}
                        """,
                        tuple(params),
                    )
                    r = cur.fetchone()
                return self._row_to_item_with_action_note(r) if r else None
            if not self._is_missing_action_note_error(exc):
                raise

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  id, disk_name, COALESCE(NULLIF(display_name,''), disk_name),
                  stage, flag_i, flag_g, in_progress_by, pid, note, note_color, shoot_date, pdf_path, pdf_path_2,
                  pdf_path_3, pdf_path_4, late_pdf_path, excel_path, orders_form_path, orders_form_path_2, orders_form_path_3, orders_form_path_4,
                  qr_roster_path, qr_orders_path
                FROM {source}
                WHERE {where}
                """,
                tuple(params),
            )
            r = cur.fetchone()
        return self._row_to_item_legacy(r) if r else None

    def list_disk_names(
        self,
        *,
        domain: Optional[str] = WORKFLOW_DOMAIN_PREPAID,
        stage: Optional[int] = None,
    ) -> List[str]:
        domain_norm = self._normalize_domain(domain) if domain is not None else None
        if domain_norm is not None and domain_norm != WORKFLOW_DOMAIN_PREPAID and not self.has_workflow_domain_columns():
            return []
        where_parts: List[str] = []
        params: List[Any] = []
        if domain_norm is not None and self.has_workflow_domain_columns():
            where_parts.append("COALESCE(NULLIF(workflow_domain,''), %s) = %s")
            params.extend([WORKFLOW_DOMAIN_PREPAID, domain_norm])
        if stage is not None:
            where_parts.append("stage=%s")
            params.append(int(stage))
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        with self.connect() as conn, conn.cursor() as cur:
            source = self._workflow_read_source()
            cur.execute(f"SELECT disk_name FROM {source} {where}", tuple(params))
            rows = cur.fetchall()
        return [str(r[0]) for r in rows if r and r[0]]

    # ---------------- Updates (used by UI) ----------------
    def update_stage(self, item_id: int, new_stage: int) -> None:
        self._update_workflow_run_fields(item_id, stage=int(new_stage))

    def upsert_from_disk_name(self, disk_name: str, stage: int) -> int:
        return self.upsert_into_domain(
            disk_name=disk_name,
            domain=WORKFLOW_DOMAIN_PREPAID,
            stage=stage,
        )

    def upsert_into_domain(
        self,
        *,
        disk_name: str,
        domain: str,
        stage: int,
        step: Optional[str] = None,
    ) -> int:
        raw = (disk_name or "").strip()
        if not raw:
            raise ValueError("disk_name cannot be empty")
        domain_norm = self._normalize_domain(domain)
        return self._upsert_normalized_item_state(
            disk_name=raw,
            display_name=normalize_display_name(raw),
            stage=int(stage),
            domain=domain_norm,
            step=(step or "").strip() or None,
            pid=parse_job_code(normalize_display_name(raw)),
            shoot_date=parse_shoot_date_from_display(normalize_display_name(raw)),
        )

    def _workflow_identity_for_disk_name(
        self,
        disk_name: str,
        *,
        domain: str = WORKFLOW_DOMAIN_PREPAID,
    ) -> Optional[tuple[int, str]]:
        raw = (disk_name or "").strip()
        if not raw:
            return None
        domain_norm = self._normalize_domain(domain)
        source = self._workflow_read_source()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, COALESCE(NULLIF(workflow_domain,''), %s)
                FROM {source}
                WHERE disk_name=%s
                  AND COALESCE(NULLIF(workflow_domain,''), %s) = %s
                LIMIT 1
                """,
                (WORKFLOW_DOMAIN_PREPAID, raw, WORKFLOW_DOMAIN_PREPAID, domain_norm),
            )
            row = cur.fetchone()
        if not row:
            return None
        return int(row[0]), self._normalize_domain(str(row[1] or WORKFLOW_DOMAIN_PREPAID))

    def update_disk_name(self, item_id: int, new_disk_name: str) -> None:
        display = normalize_display_name(new_disk_name)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET disk_name=%s,
                    display_name=%s,
                    updated_at=now()
                WHERE id=%s
                """,
                (new_disk_name, display or new_disk_name, item_id),
            )
            cur.execute(
                """
                UPDATE jobs
                SET canonical_name=%s,
                    display_name=%s,
                    pid=%s,
                    shoot_date=%s,
                    updated_at=now()
                WHERE workflow_run_id=%s
                """,
                (
                    display or new_disk_name,
                    display or new_disk_name,
                    parse_job_code(display),
                    parse_shoot_date_from_display(display),
                    item_id,
                ),
            )

    def get_unique_disk_name(
        self,
        desired_disk_name: str,
        exclude_item_id: Optional[int] = None,
        *,
        domain: str = WORKFLOW_DOMAIN_PREPAID,
    ) -> str:
        base = (desired_disk_name or "").strip()
        if not base:
            raise ValueError("desired_disk_name cannot be empty")

        domain_norm = self._normalize_domain(domain)
        with self.connect() as conn, conn.cursor() as cur:
            candidate = base
            i = 1
            while True:
                cur.execute(
                    """
                    SELECT id
                    FROM workflow_runs
                    WHERE workflow_domain=%s
                      AND disk_name=%s
                    """,
                    (domain_norm, candidate),
                )
                row = cur.fetchone()
                if not row:
                    return candidate
                if exclude_item_id is not None and int(row[0]) == int(exclude_item_id):
                    return candidate
                candidate = f"{base} ({i})"
                i += 1


    def update_flags(self, item_id: int, flag_i: Optional[bool] = None, flag_g: Optional[bool] = None) -> None:
        updates: dict[str, Any] = {}
        if flag_i is not None:
            updates["flag_i"] = bool(flag_i)
        if flag_g is not None:
            updates["flag_g"] = bool(flag_g)
        if not updates:
            return
        self._update_workflow_run_fields(item_id, **updates)

    def set_in_progress(self, item_id: int, name: Optional[str]) -> None:
        self._update_workflow_run_fields(item_id, in_progress_by=name)

    def set_note(self, item_id: int, note: Optional[str]) -> None:
        self._update_workflow_run_fields(item_id, note=note)

    def set_action_note(self, item_id: int, action_note: Optional[str]) -> None:
        self._update_workflow_run_fields(item_id, action_note=action_note)

    def set_contact(
        self,
        item_id: int,
        *,
        contact_name: Optional[str],
        contact_email: Optional[str],
        contact_phone: Optional[str],
    ) -> None:
        self._update_job_fields(
            item_id,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
        )

    def set_school_email_sent(self, item_id: int, recipient: Optional[str]) -> None:
        if self._workflow_domain_for_item(item_id) not in {WORKFLOW_DOMAIN_PROOFING, WORKFLOW_DOMAIN_YEARBOOK}:
            return
        run_id = self._ensure_normalized_workflow_run(item_id)
        if run_id is None:
            return
        recipient_value = str(recipient or "").strip() or None
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET school_email_sent_at=now(),
                    school_email_recipient=%s,
                    updated_at=now()
                WHERE id=%s
                RETURNING school_email_sent_at
                """,
                (recipient_value, run_id),
            )
            row = cur.fetchone()
        sent_at = row[0] if row else None
        if sent_at is not None:
            self.record_workflow_event(
                item_id,
                "school_email_sent",
                source="ui",
                event_key=f"school_email_sent:{run_id}:{sent_at}",
                payload={"recipient": recipient_value},
            )

    def clear_school_email_sent(self, item_id: int) -> None:
        if self._workflow_domain_for_item(item_id) not in {WORKFLOW_DOMAIN_PROOFING, WORKFLOW_DOMAIN_YEARBOOK}:
            return
        self._update_workflow_run_fields(
            item_id,
            school_email_sent_at=None,
            school_email_recipient=None,
        )

    def get_school_email_status(self, item_id: int) -> tuple[Optional[Any], str]:
        run_id = self._ensure_normalized_workflow_run(item_id)
        if run_id is None:
            return None, ""
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT school_email_sent_at, school_email_recipient
                FROM workflow_runs
                WHERE id=%s
                """,
                (run_id,),
            )
            row = cur.fetchone()
        if not row:
            return None, ""
        return row[0], str(row[1] or "").strip()

    def set_contact_if_empty(
        self,
        item_id: int,
        *,
        contact_name: Optional[str],
        contact_email: Optional[str],
        contact_phone: Optional[str],
    ) -> bool:
        name_val = (str(contact_name or "").strip() or None)
        email_val = (str(contact_email or "").strip() or None)
        phone_val = (str(contact_phone or "").strip() or None)
        if not (name_val or email_val or phone_val):
            return False
        run_id = self._ensure_normalized_workflow_run(item_id)
        if run_id is None:
            return False
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET
                  contact_name = COALESCE(NULLIF(BTRIM(contact_name), ''), %s::text),
                  contact_email = COALESCE(NULLIF(BTRIM(contact_email), ''), %s::text),
                  contact_phone = COALESCE(NULLIF(BTRIM(contact_phone), ''), %s::text),
                  updated_at = now()
                WHERE workflow_run_id=%s
                  AND (
                    (NULLIF(BTRIM(contact_name), '') IS NULL AND %s::text IS NOT NULL)
                    OR (NULLIF(BTRIM(contact_email), '') IS NULL AND %s::text IS NOT NULL)
                    OR (NULLIF(BTRIM(contact_phone), '') IS NULL AND %s::text IS NOT NULL)
                  )
                """,
                (name_val, email_val, phone_val, run_id, name_val, email_val, phone_val),
            )
            changed = bool(cur.rowcount)
        if changed:
            self._ensure_normalized_workflow_run(item_id)
        return changed

    def set_note_color(self, item_id: int, color: Optional[str]) -> None:
        self._update_workflow_run_fields(item_id, note_color=color)

    def set_pdf_path(self, item_id: int, pdf_path: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "pdf_path"):
            return
        self._set_workflow_detail_column(item_id, "pdf_path", pdf_path)

    def set_pdf_path_2(self, item_id: int, pdf_path_2: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "pdf_path_2"):
            return
        self._set_workflow_detail_column(item_id, "pdf_path_2", pdf_path_2)

    def set_pdf_path_3(self, item_id: int, pdf_path_3: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "pdf_path_3"):
            return
        self._set_workflow_detail_column(item_id, "pdf_path_3", pdf_path_3)

    def set_pdf_path_4(self, item_id: int, pdf_path_4: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "pdf_path_4"):
            return
        self._set_workflow_detail_column(item_id, "pdf_path_4", pdf_path_4)

    def set_late_pdf_path(self, item_id: int, late_pdf_path: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "late_pdf_path"):
            return
        self._set_workflow_detail_column(item_id, "late_pdf_path", late_pdf_path)

    def set_excel_path(self, item_id: int, excel_path: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "excel_path"):
            return
        self._set_workflow_detail_column(item_id, "excel_path", excel_path)

    def set_orders_form_path(self, item_id: int, orders_form_path: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "orders_form_path"):
            return
        self._set_workflow_detail_column(item_id, "orders_form_path", orders_form_path)

    def set_orders_form_path_2(self, item_id: int, orders_form_path_2: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "orders_form_path_2"):
            return
        self._set_workflow_detail_column(item_id, "orders_form_path_2", orders_form_path_2)

    def set_orders_form_path_3(self, item_id: int, orders_form_path_3: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "orders_form_path_3"):
            return
        self._set_workflow_detail_column(item_id, "orders_form_path_3", orders_form_path_3)

    def set_orders_form_path_4(self, item_id: int, orders_form_path_4: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "orders_form_path_4"):
            return
        self._set_workflow_detail_column(item_id, "orders_form_path_4", orders_form_path_4)

    def set_qr_roster_path(self, item_id: int, qr_roster_path: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "qr_roster_path"):
            return
        self._set_workflow_detail_column(item_id, "qr_roster_path", qr_roster_path)

    def set_qr_orders_path(self, item_id: int, qr_orders_path: Optional[str]) -> None:
        if not self._workflow_detail_column_allowed(item_id, "qr_orders_path"):
            return
        self._set_workflow_detail_column(item_id, "qr_orders_path", qr_orders_path)

    def order_import_exists(
        self,
        pid: str,
        order_no: str,
        *,
        source: str = ORDER_IMPORT_SOURCE_GODADDY,
    ) -> bool:
        source = str(source or ORDER_IMPORT_SOURCE_GODADDY).strip() or ORDER_IMPORT_SOURCE_GODADDY
        event_key = self._order_import_event_key(source, pid, order_no)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM workflow_events
                WHERE event_key=%s
                LIMIT 1
                """,
                (event_key,),
            )
            row = cur.fetchone()
        return bool(row)

    def record_order_import(
        self,
        *,
        source: str = ORDER_IMPORT_SOURCE_GODADDY,
        pid: str,
        order_no: str,
        message_id: Optional[str],
        item_id: Optional[int],
        gmail_internal_at: Optional[Any] = None,
        late_cutoff_at: Optional[Any] = None,
        is_late: bool = False,
        pdf_output_path: Optional[str] = None,
    ) -> None:
        source = str(source or ORDER_IMPORT_SOURCE_GODADDY).strip() or ORDER_IMPORT_SOURCE_GODADDY
        if item_id is None:
            return
        self.record_workflow_event(
            item_id,
            "order_imported",
            source=source,
            event_key=self._order_import_event_key(source, pid, order_no),
            payload={
                "pid": pid,
                "order_no": order_no,
                "message_id": message_id,
                "item_id": int(item_id),
                "is_late": bool(is_late),
                "pdf_output_path": pdf_output_path,
                "gmail_internal_at": str(gmail_internal_at) if gmail_internal_at is not None else None,
                "late_cutoff_at": str(late_cutoff_at) if late_cutoff_at is not None else None,
            },
        )

    def delete_order_import_record(
        self,
        pid: str,
        order_no: str,
        *,
        source: str = ORDER_IMPORT_SOURCE_GODADDY,
    ) -> None:
        source = str(source or ORDER_IMPORT_SOURCE_GODADDY).strip() or ORDER_IMPORT_SOURCE_GODADDY
        event_key = self._order_import_event_key(source, pid, order_no)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM workflow_events
                WHERE event_key=%s
                """,
                (event_key,),
            )

    def restore_item_snapshot(self, snapshot: dict[str, Any] | None, *, disk_name_override: Optional[str] = None) -> str | None:
        if not snapshot:
            return None
        disk_name = (disk_name_override or snapshot.get("disk_name") or "").strip()
        if not disk_name:
            return None
        item_id = int(snapshot["id"])
        display_name = str(snapshot.get("display_name") or disk_name)
        workflow_domain = self._normalize_domain(str(snapshot.get("workflow_domain") or WORKFLOW_DOMAIN_PREPAID))
        workflow_step = (str(snapshot.get("workflow_step") or "").strip() or None)
        school_email_sent_at = snapshot.get("school_email_sent_at")
        school_email_recipient = snapshot.get("school_email_recipient")

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM workflow_runs
                WHERE workflow_domain=%s
                  AND disk_name=%s
                  AND id<>%s
                """,
                (workflow_domain, disk_name, item_id),
            )
            if cur.fetchone() is not None:
                raise RuntimeError(f"Cannot restore {disk_name!r}; that name is already used in {workflow_domain}.")

            cur.execute(
                """
                INSERT INTO workflow_runs(
                    id, workflow_domain, workflow_step, disk_name, display_name,
                    stage, sort_key, flag_i, flag_g, in_progress_by, note, action_note,
                    note_color, school_email_sent_at, school_email_recipient,
                    archived_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    NULL, now()
                )
                ON CONFLICT (id) DO UPDATE SET
                    workflow_domain=EXCLUDED.workflow_domain,
                    workflow_step=EXCLUDED.workflow_step,
                    disk_name=EXCLUDED.disk_name,
                    display_name=EXCLUDED.display_name,
                    stage=EXCLUDED.stage,
                    sort_key=EXCLUDED.sort_key,
                    flag_i=EXCLUDED.flag_i,
                    flag_g=EXCLUDED.flag_g,
                    in_progress_by=EXCLUDED.in_progress_by,
                    note=EXCLUDED.note,
                    action_note=EXCLUDED.action_note,
                    note_color=EXCLUDED.note_color,
                    school_email_sent_at=EXCLUDED.school_email_sent_at,
                    school_email_recipient=EXCLUDED.school_email_recipient,
                    archived_at=NULL,
                    updated_at=now()
                """,
                (
                    item_id,
                    workflow_domain,
                    workflow_step,
                    disk_name,
                    display_name,
                    int(snapshot.get("stage", 1)),
                    int(snapshot.get("sort_key") or 0),
                    bool(snapshot.get("flag_i")),
                    bool(snapshot.get("flag_g")),
                    snapshot.get("in_progress_by"),
                    snapshot.get("note"),
                    snapshot.get("action_note"),
                    snapshot.get("note_color"),
                    school_email_sent_at,
                    school_email_recipient,
                ),
            )
            cur.execute(
                """
                INSERT INTO jobs(
                    workflow_run_id, pid, canonical_name, display_name,
                    shoot_date, contact_name, contact_email, contact_phone, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (workflow_run_id) DO UPDATE SET
                    pid=EXCLUDED.pid,
                    canonical_name=EXCLUDED.canonical_name,
                    display_name=EXCLUDED.display_name,
                    shoot_date=EXCLUDED.shoot_date,
                    contact_name=EXCLUDED.contact_name,
                    contact_email=EXCLUDED.contact_email,
                    contact_phone=EXCLUDED.contact_phone,
                    updated_at=now()
                """,
                (
                    item_id,
                    snapshot.get("pid"),
                    display_name,
                    display_name,
                    snapshot.get("shoot_date"),
                    snapshot.get("contact_name"),
                    snapshot.get("contact_email"),
                    snapshot.get("contact_phone"),
                ),
            )
            cur.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence('workflow_runs', 'id'),
                    GREATEST(COALESCE((SELECT MAX(id) FROM workflow_runs), 1), 1),
                    true
                )
                """
            )

        for column_name in _ASSET_COLUMN_TO_TYPE_SLOT:
            self.set_workflow_detail_value(item_id, column_name, snapshot.get(column_name))

        return disk_name

    def delete_item(self, item_id: int) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET archived_at=COALESCE(archived_at, now()),
                    updated_at=now()
                WHERE id=%s
                """,
                (item_id,),
            )

    # ---------------- Maintenance ----------------
    def backfill_shoot_dates(self) -> int:
        """
        Fills shoot_date for rows where it's NULL by parsing display_name in Python.
        Returns number updated.
        """
        source = self._workflow_read_source()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, COALESCE(NULLIF(display_name,''), disk_name)
                FROM {source}
                WHERE shoot_date IS NULL
                """
            )
            rows = cur.fetchall()

            updated = 0
            for item_id, disp in rows:
                sdate = parse_shoot_date_from_display(disp)
                if sdate:
                    self._update_job_fields(int(item_id), shoot_date=sdate)
                    updated += 1
        return updated
