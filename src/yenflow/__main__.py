"""Entrypoint for `python -m yenflow`."""

from __future__ import annotations

import sys

from yenflow.cli import main

if __name__ == "__main__":
    sys.exit(main() or 0)
