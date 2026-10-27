"""Stable workspace paths shared by feature folders; no runtime IO."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
