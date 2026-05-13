from __future__ import annotations

from typing import Callable, Optional


def emit_status(message: str, progress_callback: Optional[Callable[[str], None]] = None) -> None:
    if progress_callback:
        progress_callback(message)
    print(message)

