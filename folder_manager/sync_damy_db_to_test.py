from __future__ import annotations

import argparse
import os
from typing import Sequence

from .config import DB_HOST, DB_PASS, DB_PORT, DB_USER
from .migrate_workflow_v2 import migrate


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync legacy DAMY workflow data into a v2 test database."
    )
    parser.add_argument("--source-db", default=os.getenv("DAMY_SOURCE_DB_NAME", "damy_workflow"))
    parser.add_argument("--target-db", default=os.getenv("DAMY_TARGET_DB_NAME", "DAMY_TEST"))
    parser.add_argument("--host", default=os.getenv("DAMY_DB_HOST", DB_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("DAMY_DB_PORT", str(DB_PORT))))
    parser.add_argument("--user", default=os.getenv("DAMY_DB_USER", DB_USER))
    parser.add_argument("--password", default=os.getenv("DAMY_DB_PASS", DB_PASS))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.source_db == args.target_db:
        raise RuntimeError("Refusing to sync: source and target database names are the same.")
    migrate(
        args.source_db,
        args.target_db,
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
    )
    print(f"[OK] Synced {args.source_db} -> {args.target_db} using v2 workflow schema.")


if __name__ == "__main__":
    main()
