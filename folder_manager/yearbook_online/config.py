"""Shared configuration values for the DAMY Yearbook workspace."""

from __future__ import annotations

import os
from pathlib import Path

# Root directory containing yearbook workflow folders.
BASE_DIRECTORY = Path(os.getenv("DAMY_YEARBOOK_BASE_DIR", os.getenv("DAMY_BASE_DIR", r"T:\DAMY")))

__all__ = ["BASE_DIRECTORY"]
