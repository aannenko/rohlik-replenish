#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = [
#     "mcp>=2.3,<3",
#     "keyring>=25.7,<26",
#     "platformdirs>=4.13,<5",
# ]
# ///
"""Rohlik replenishment skill — engine entry point.

Run as `uv run replenish.py <command>` (see README.md). The engine is the `engine/` package next to this file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
