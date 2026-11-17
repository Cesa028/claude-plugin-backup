"""Plugin Backup per Claude Code - costanti e utilità condivise."""
from __future__ import annotations

import os
import subprocess

APP_NAME = "Plugin Backup per Claude Code"
APP_SHORT = "Plugin Backup"
APP_ID = "ClaudePluginBackup"
APP_VERSION = "1.1.0"
APP_AUTHOR = "Cesa028"
APP_URL = "https://github.com/Cesa028/claude-plugin-backup"
APPDATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_ID)
os.makedirs(APPDATA, exist_ok=True)
CONFIG = os.path.join(APPDATA, "settings.json")
LOG_FILE = os.path.join(APPDATA, "registro.log")
ERROR_LOG = os.path.join(APPDATA, "errori.log")


def default_backup_dir() -> str:
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "Claude Plugin Backup")


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            s = f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            return s.replace(".", ",")
        n /= 1024
    return "0 B"


def open_path(path) -> None:
    if path and os.path.exists(path):
        os.startfile(os.path.abspath(path))


def reveal(path) -> None:
    if path and os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(os.path.abspath(path))])
