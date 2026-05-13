"""
Command-line entry point to launch the DAMY workflow user interface.
"""

from __future__ import annotations

from .ui import run_app


def main() -> None:
    run_app()


if __name__ == "__main__":
    main()
