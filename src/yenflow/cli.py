"""Yenflow command line interface."""

from __future__ import annotations

import sys
from typing import NoReturn

from gflow_cli.cli import main as gflow_main


def main() -> NoReturn | None:
    """CLI entry point for `yenflow`."""
    return gflow_main()


if __name__ == "__main__":
    sys.exit(main() or 0)
