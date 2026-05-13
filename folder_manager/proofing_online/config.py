"""
Shared configuration values for the DAMY Online toolchain.
"""

from __future__ import annotations

import os
from pathlib import Path

# Root directory containing the production photo workflow folders.
BASE_DIRECTORY = Path(os.getenv("DAMY_PROOF_BASE_DIR", os.getenv("DAMY_BASE_DIR", r"T:\DAMY")))

__all__ = ["BASE_DIRECTORY"]
