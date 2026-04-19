"""Install/uninstall a launchd agent for periodic sync."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "app.sticky.agent"
LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS / f"{LABEL}.plist"


def _sticky_binary() -> str:
    """Path to the installed `sticky` entry point."""
    # When installed via `uv tool install` the script is on PATH as `sticky`.
    # We resolve via `sys.argv[0]` fallback to the current interpreter.
    from shutil import which

    resolved = which("sticky")
    if resolved:
        return resolved
    return sys.argv[0] or "sticky"


def build_plist(interval_seconds: int = 12 * 3600) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [_sticky_binary(), "sync", "--quiet"],
        "StartInterval": interval_seconds,
        "RunAtLoad": False,
        "StandardOutPath": str(Path.home() / ".sticky" / "daemon.log"),
        "StandardErrorPath": str(Path.home() / ".sticky" / "daemon.err"),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    }


def install(interval_seconds: int = 12 * 3600) -> Path:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(build_plist(interval_seconds), fh)
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], check=False)
    subprocess.run(["launchctl", "load", str(PLIST_PATH)], check=True)
    return PLIST_PATH


def uninstall() -> bool:
    if not PLIST_PATH.exists():
        return False
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], check=False)
    PLIST_PATH.unlink()
    return True


def is_installed() -> bool:
    return PLIST_PATH.exists()
