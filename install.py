#!/usr/bin/env python3
"""GhostAP installer launcher:  sudo python3 install.py   (add --dry-run to preview)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "ghostap"))
import installer  # noqa: E402

sys.exit(installer.run())
