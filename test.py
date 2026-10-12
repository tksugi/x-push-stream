"""Opt-in live capture; credentials are supplied through the environment."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).parent / "examples" / "capture_notifications.py"),
        run_name="__main__",
    )
