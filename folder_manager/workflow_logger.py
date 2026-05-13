from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    NY_TZ = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    NY_TZ = None


def _safe(s: str, max_len: int = 40) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s[:max_len] if max_len else s


@dataclass(frozen=True)
class AuditEvent:
    action: str            # MOVE, FLAGI, FLAGG, INPROGRESS, CLEARIP
    item_id: int
    disk_name: str
    old_stage: Optional[int] = None
    new_stage: Optional[int] = None
    value: Optional[str] = None  # e.g. ON/OFF, name, etc.


def write_event(event: AuditEvent, *, base_dir: str) -> None:
    """
    One TXT per event:
      <base_dir>\\_workflow_log\\YYYY-MM-DD\\<timestamp>_<pc>_<action>_<id>.txt

    File content is intentionally minimal.
    """
    try:
        # Day folder
        now = datetime.now(NY_TZ) if NY_TZ else datetime.now()
        day_dir = Path(base_dir) / "_workflow_log" / f"{now:%Y-%m-%d}"
        day_dir.mkdir(parents=True, exist_ok=True)

        pc = os.environ.get("COMPUTERNAME", "PC")

        # Unique filename
        ts = f"{now:%Y-%m-%d_%H-%M-%S}.{now.microsecond//1000:03d}"
        fname = f"{ts}_{_safe(pc,18)}_{_safe(event.action,10)}_{event.item_id:09d}.txt"
        path = day_dir / fname

        # Minimal content
        tz_label = now.strftime("%Z") if now.tzinfo else "LOCAL"
        when = f"{now:%Y-%m-%d %H:%M:%S} {tz_label}"

        if event.action == "MOVE":
            line2 = (
                f'Moved "{event.disk_name}" from stage {event.old_stage} to stage {event.new_stage}'
            )
        elif event.action in ("FLAGI", "FLAGG"):
            which = "I" if event.action == "FLAGI" else "G"
            line2 = f'FLAG {which} = {event.value} for "{event.disk_name}"'
        elif event.action == "INPROGRESS":
            line2 = f'In Progress = "{event.value}" for "{event.disk_name}"'
        elif event.action == "CLEARIP":
            line2 = f'In Progress CLEARED for "{event.disk_name}"'
        else:
            # fallback
            line2 = f'{event.action} "{event.disk_name}"'

        path.write_text(when + "\n" + line2 + "\n", encoding="utf-8")

    except Exception:
        # Never crash app because logging failed
        pass
