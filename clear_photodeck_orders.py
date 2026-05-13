"""
Clear all PhotoDeck paid order records from the database.

Deletes:
  - proofing_paid_order_assets  (all rows)
  - workflow_events              (rows with event_key LIKE 'order_import:photodeck_paid:%')
"""

import sys
import psycopg

DB_HOST = "192.168.1.208"
DB_NAME = "damy_workflow_v2"
DB_USER = "damy_app"
DB_PASS = "2357"
DB_PORT = 5432

CONNINFO = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASS}"
EVENT_KEY_PREFIX = "order_import:photodeck_paid:%"


def main() -> None:
    with psycopg.connect(CONNINFO) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM proofing_paid_order_assets")
        asset_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM workflow_events WHERE event_key LIKE %s", (EVENT_KEY_PREFIX,))
        event_count = cur.fetchone()[0]

    print(f"Records to delete:")
    print(f"  proofing_paid_order_assets : {asset_count}")
    print(f"  workflow_events (photodeck): {event_count}")

    if asset_count == 0 and event_count == 0:
        print("Nothing to delete.")
        return

    answer = input("\nType YES to confirm deletion: ").strip()
    if answer != "YES":
        print("Cancelled.")
        return

    with psycopg.connect(CONNINFO) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM proofing_paid_order_assets")
        deleted_assets = cur.rowcount
        cur.execute("DELETE FROM workflow_events WHERE event_key LIKE %s", (EVENT_KEY_PREFIX,))
        deleted_events = cur.rowcount

    print(f"\nDeleted:")
    print(f"  proofing_paid_order_assets : {deleted_assets}")
    print(f"  workflow_events            : {deleted_events}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)
