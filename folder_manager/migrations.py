from __future__ import annotations

from dataclasses import dataclass
from typing import List, Set

import psycopg

from .db import STAGES


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


# Prepaid stages come from stages.json; proofing/yearbook code currently use up to 10.
MIN_STAGE = min((stage.stage for stage in STAGES), default=1)
MAX_STAGE = max(max((stage.stage for stage in STAGES), default=1), 10)


MIGRATIONS: List[Migration] = [
    Migration(
        version=1,
        name="create_v2_workflow_schema",
        sql=f"""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workflow_runs (
            id                       BIGSERIAL PRIMARY KEY,
            workflow_domain          TEXT NOT NULL CHECK (workflow_domain IN ('prepaid', 'proofing', 'yearbook')),
            workflow_step            TEXT,
            disk_name                TEXT NOT NULL,
            display_name             TEXT NOT NULL DEFAULT '',
            stage                    SMALLINT NOT NULL CHECK (stage BETWEEN {MIN_STAGE} AND {MAX_STAGE}),
            sort_key                 NUMERIC(20,10) NOT NULL DEFAULT 0,
            flag_i                   BOOLEAN NOT NULL DEFAULT FALSE,
            flag_g                   BOOLEAN NOT NULL DEFAULT FALSE,
            in_progress_by           TEXT,
            note                     TEXT,
            action_note              TEXT,
            note_color               TEXT,
            school_email_sent_at     TIMESTAMPTZ,
            school_email_recipient   TEXT,
            archived_at              TIMESTAMPTZ,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT workflow_runs_domain_disk_name_uniq UNIQUE (workflow_domain, disk_name)
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_runs_domain_stage_sort
        ON workflow_runs(workflow_domain, stage, sort_key);

        CREATE INDEX IF NOT EXISTS idx_workflow_runs_domain_shoot_lookup
        ON workflow_runs(workflow_domain, disk_name)
        WHERE archived_at IS NULL;

        CREATE TABLE IF NOT EXISTS jobs (
            id                  BIGSERIAL PRIMARY KEY,
            workflow_run_id     BIGINT NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
            pid                 TEXT,
            canonical_name      TEXT NOT NULL,
            display_name        TEXT,
            shoot_date          DATE,
            contact_name        TEXT,
            contact_email       TEXT,
            contact_phone       TEXT,
            source              TEXT NOT NULL DEFAULT 'workflow_runs',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_pid ON jobs(pid);
        CREATE INDEX IF NOT EXISTS idx_jobs_shoot_date ON jobs(shoot_date);
        CREATE INDEX IF NOT EXISTS idx_jobs_canonical_name ON jobs(canonical_name);

        CREATE TABLE IF NOT EXISTS workflow_assets (
            id                  BIGSERIAL PRIMARY KEY,
            workflow_run_id     BIGINT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            asset_type          TEXT NOT NULL CHECK (btrim(asset_type) <> ''),
            slot                SMALLINT NOT NULL DEFAULT 1,
            path                TEXT NOT NULL CHECK (btrim(path) <> ''),
            metadata            JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT workflow_assets_run_type_slot_uniq UNIQUE (workflow_run_id, asset_type, slot)
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_assets_type ON workflow_assets(asset_type);

        CREATE TABLE IF NOT EXISTS workflow_events (
            id                  BIGSERIAL PRIMARY KEY,
            workflow_run_id     BIGINT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            event_type          TEXT NOT NULL CHECK (btrim(event_type) <> ''),
            source              TEXT,
            event_key           TEXT NOT NULL UNIQUE,
            payload             JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_events_run_type_created
        ON workflow_events(workflow_run_id, event_type, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_workflow_events_payload_gin
        ON workflow_events USING GIN(payload);

        CREATE TABLE IF NOT EXISTS parent_delivery_contacts (
            id                BIGSERIAL PRIMARY KEY,
            workflow_run_id   BIGINT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            job_disk_name     TEXT NOT NULL DEFAULT '',
            child_name        TEXT NOT NULL DEFAULT '',
            class_name        TEXT NOT NULL DEFAULT '',
            page_index        INTEGER NOT NULL DEFAULT 0,
            password          TEXT NOT NULL DEFAULT '',
            parent_phone      TEXT NOT NULL DEFAULT '',
            parent_email      TEXT NOT NULL DEFAULT '',
            note              TEXT NOT NULL DEFAULT '',
            preview_url       TEXT NOT NULL DEFAULT '',
            local_image_path  TEXT NOT NULL DEFAULT '',
            source_pdf_path   TEXT NOT NULL DEFAULT '',
            order_url         TEXT NOT NULL DEFAULT '',
            object_key        TEXT NOT NULL DEFAULT '',
            mms_sid           TEXT NOT NULL DEFAULT '',
            mms_sent_at       TEXT NOT NULL DEFAULT '',
            mms_error         TEXT NOT NULL DEFAULT '',
            email_message_id  TEXT NOT NULL DEFAULT '',
            email_sent_at     TEXT NOT NULL DEFAULT '',
            email_error       TEXT NOT NULL DEFAULT '',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT parent_delivery_run_pdf_page_uniq UNIQUE (workflow_run_id, source_pdf_path, page_index)
        );

        CREATE INDEX IF NOT EXISTS idx_parent_delivery_run_status
        ON parent_delivery_contacts(workflow_run_id, child_name, class_name);

        CREATE TABLE IF NOT EXISTS proofing_paid_order_assets (
            id                BIGSERIAL PRIMARY KEY,
            workflow_run_id   BIGINT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            disk_name         TEXT NOT NULL DEFAULT '',
            pid               TEXT NOT NULL DEFAULT '',
            order_no          TEXT NOT NULL DEFAULT '',
            message_id        TEXT NOT NULL DEFAULT '',
            asset_type        TEXT NOT NULL DEFAULT '',
            original_id       TEXT NOT NULL DEFAULT '',
            proof_id          TEXT NOT NULL DEFAULT '',
            path              TEXT NOT NULL DEFAULT '',
            source_path       TEXT NOT NULL DEFAULT '',
            order_pdf_path    TEXT NOT NULL DEFAULT '',
            label             TEXT NOT NULL DEFAULT '',
            package           TEXT NOT NULL DEFAULT '',
            addons            JSONB NOT NULL DEFAULT '[]'::jsonb,
            background        TEXT NOT NULL DEFAULT '',
            quantity          INTEGER NOT NULL DEFAULT 1,
            asset_status      TEXT NOT NULL DEFAULT 'stage6' CHECK (asset_status IN ('stage6', 'stage7', 'archived')),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT proofing_paid_assets_path_uniq UNIQUE (path)
        );

        CREATE INDEX IF NOT EXISTS idx_proofing_paid_assets_status
        ON proofing_paid_order_assets(asset_status, asset_type, workflow_run_id);

        CREATE INDEX IF NOT EXISTS idx_proofing_paid_assets_order_group
        ON proofing_paid_order_assets(workflow_run_id, order_no, asset_type, original_id, proof_id);

        CREATE OR REPLACE VIEW workflow_items_normalized_view AS
        WITH asset_pivot AS (
            SELECT
                workflow_run_id,
                MAX(path) FILTER (WHERE asset_type = 'pdf' AND slot = 1) AS pdf_path,
                MAX(path) FILTER (WHERE asset_type = 'pdf' AND slot = 2) AS pdf_path_2,
                MAX(path) FILTER (WHERE asset_type = 'pdf' AND slot = 3) AS pdf_path_3,
                MAX(path) FILTER (WHERE asset_type = 'pdf' AND slot = 4) AS pdf_path_4,
                MAX(path) FILTER (WHERE asset_type = 'late_pdf' AND slot = 1) AS late_pdf_path,
                MAX(path) FILTER (WHERE asset_type = 'excel' AND slot = 1) AS excel_path,
                MAX(path) FILTER (WHERE asset_type = 'orders_form' AND slot = 1) AS orders_form_path,
                MAX(path) FILTER (WHERE asset_type = 'orders_form' AND slot = 2) AS orders_form_path_2,
                MAX(path) FILTER (WHERE asset_type = 'orders_form' AND slot = 3) AS orders_form_path_3,
                MAX(path) FILTER (WHERE asset_type = 'orders_form' AND slot = 4) AS orders_form_path_4,
                MAX(path) FILTER (WHERE asset_type = 'qr_roster' AND slot = 1) AS qr_roster_path,
                MAX(path) FILTER (WHERE asset_type = 'qr_orders' AND slot = 1) AS qr_orders_path
            FROM workflow_assets
            GROUP BY workflow_run_id
        )
        SELECT
            wr.id,
            wr.disk_name,
            COALESCE(NULLIF(j.display_name, ''), NULLIF(wr.display_name, ''), j.canonical_name, wr.disk_name) AS display_name,
            wr.stage,
            wr.sort_key,
            wr.flag_i,
            wr.flag_g,
            wr.in_progress_by,
            wr.updated_at,
            j.pid,
            wr.note,
            wr.action_note,
            j.contact_name,
            j.contact_email,
            j.contact_phone,
            wr.note_color,
            j.shoot_date,
            wr.workflow_domain,
            wr.workflow_step,
            ap.pdf_path,
            ap.pdf_path_2,
            ap.pdf_path_3,
            ap.pdf_path_4,
            ap.late_pdf_path,
            ap.excel_path,
            ap.orders_form_path,
            ap.orders_form_path_2,
            ap.orders_form_path_3,
            ap.orders_form_path_4,
            ap.qr_roster_path,
            ap.qr_orders_path,
            wr.school_email_sent_at,
            wr.school_email_recipient
        FROM workflow_runs wr
        LEFT JOIN jobs j ON j.workflow_run_id = wr.id
        LEFT JOIN asset_pivot ap ON ap.workflow_run_id = wr.id
        WHERE wr.archived_at IS NULL;
        """,
    ),
]


def ensure_stage_constraint(conninfo: str) -> None:
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('workflow_runs');")
            if cur.fetchone()[0] is None:
                return
            cur.execute("ALTER TABLE workflow_runs DROP CONSTRAINT IF EXISTS workflow_runs_stage_check;")
            cur.execute(
                f"""
                ALTER TABLE workflow_runs
                ADD CONSTRAINT workflow_runs_stage_check
                CHECK (stage BETWEEN {MIN_STAGE} AND {MAX_STAGE});
                """
            )
        conn.commit()


def run_migrations(conninfo: str, *, statement_timeout_seconds: int = 30) -> None:
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{int(statement_timeout_seconds)}s'")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL,
                    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            cur.execute("SELECT version FROM schema_migrations;")
            applied: Set[int] = {row[0] for row in cur.fetchall()}

        for migration in sorted(MIGRATIONS, key=lambda x: x.version):
            if migration.version in applied:
                continue
            with conn.cursor() as cur:
                cur.execute(migration.sql)
                cur.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (%s, %s);",
                    (migration.version, migration.name),
                )
        conn.commit()

    ensure_stage_constraint(conninfo)


def ensure_base_directory(conninfo: str, base_dir: str) -> None:
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings(key, value)
                VALUES ('base_directory', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
                """,
                (base_dir,),
            )
        conn.commit()
