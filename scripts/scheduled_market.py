"""Stable scheduled-market.ps1 command; never runs during import."""
from launch.scheduled import run_cli

if __name__ == '__main__':
    raise SystemExit(run_cli())
