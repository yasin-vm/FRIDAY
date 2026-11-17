"""Manages Windows startup registry configuration for FRIDAY."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_logger = logging.getLogger("friday.startup")
_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "FRIDAY_Assistant"


def is_startup_enabled() -> bool:
    """Check if FRIDAY is registered to run on Windows startup."""
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, _APP_NAME)
            return bool(value)
    except (FileNotFoundError, OSError):
        return False


def enable_startup() -> bool:
    """Register FRIDAY to start automatically on Windows login."""
    if os.name != "nt":
        return False
    import winreg

    friday_script = Path(__file__).resolve().with_name("friday.py")
    python_exe = sys.executable
    # Use pythonw if available so it launches without an intrusive console window
    pythonw = Path(python_exe).with_name("pythonw.exe")
    executable = str(pythonw) if pythonw.is_file() else python_exe

    command = f'"{executable}" "{friday_script}"'
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, command)
        _logger.info("FRIDAY registered for Windows startup: %s", command)
        return True
    except OSError as error:
        _logger.exception("Failed to enable Windows startup: %s", error)
        return False


def disable_startup() -> bool:
    """Remove FRIDAY from Windows startup registry."""
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _APP_NAME)
        _logger.info("FRIDAY removed from Windows startup registry.")
        return True
    except FileNotFoundError:
        return True
    except OSError as error:
        _logger.exception("Failed to remove Windows startup entry: %s", error)
        return False
