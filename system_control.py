"""Scoped PC Control and System Actions for FRIDAY."""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
from ctypes import wintypes
from datetime import datetime

_logger = logging.getLogger("friday.system")

# Virtual key codes for multimedia & volume
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002


if os.name == "nt":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", wintypes.BYTE),
            ("BatteryFlag", wintypes.BYTE),
            ("BatteryLifePercent", wintypes.BYTE),
            ("SystemStatusFlag", wintypes.BYTE),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("sullAvailExtendedVirtual", ctypes.c_uint64),
        ]


def _press_key(vk: int, repeat: int = 1) -> None:
    if os.name != "nt":
        return
    for _ in range(repeat):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def get_battery_status() -> str:
    """Return formatted battery percentage and AC charging state."""
    if os.name != "nt":
        return "Battery monitoring is only supported on Windows."
    sps = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        return "Could not retrieve battery information from the system."
    if sps.BatteryLifePercent == 255:
        return "No battery detected (running on AC power)."

    charging_state = "charging" if sps.ACLineStatus == 1 else "discharging on battery"
    return f"Battery is at {sps.BatteryLifePercent}%, currently {charging_state}."


def get_memory_status() -> str:
    """Return current RAM usage and availability."""
    if os.name != "nt":
        return "Memory monitoring is only supported on Windows."
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        return "Could not retrieve system memory statistics."

    total_gb = ms.ullTotalPhys / (1024**3)
    avail_gb = ms.ullAvailPhys / (1024**3)
    used_gb = total_gb - avail_gb
    return (
        f"RAM load is at {ms.dwMemoryLoad}%. Using {used_gb:.1f} GB out of "
        f"{total_gb:.1f} GB total ({avail_gb:.1f} GB free)."
    )


def adjust_volume(increase: bool, steps: int = 5) -> str:
    vk = VK_VOLUME_UP if increase else VK_VOLUME_DOWN
    _press_key(vk, repeat=max(1, steps))
    action = "Increased" if increase else "Decreased"
    return f"{action} system volume."


def toggle_mute() -> str:
    _press_key(VK_VOLUME_MUTE)
    return "Toggled system audio mute."


def control_media(action: str) -> str:
    if action == "play_pause":
        _press_key(VK_MEDIA_PLAY_PAUSE)
        return "Sent Play/Pause media command."
    if action == "next":
        _press_key(VK_MEDIA_NEXT_TRACK)
        return "Skipped to next track."
    if action == "previous":
        _press_key(VK_MEDIA_PREV_TRACK)
        return "Returned to previous track."
    return "Unknown media command."


def launch_application(app_key: str) -> tuple[bool, str]:
    """Launch common desktop applications safely."""
    apps = {
        "notepad": ("notepad.exe", "Notepad"),
        "calculator": ("calc.exe", "Calculator"),
        "spotify": ("spotify:", "Spotify"),
        "task manager": ("taskmgr.exe", "Task Manager"),
        "settings": ("ms-settings:", "Windows Settings"),
        "file explorer": ("explorer.exe", "File Explorer"),
        "browser": ("https://www.google.com", "Default Web Browser"),
        "terminal": ("powershell.exe", "PowerShell Terminal"),
        "vscode": ("code", "Visual Studio Code"),
        "camera": ("microsoft.windows.camera:", "Camera"),
    }
    target, display_name = apps.get(app_key, (None, None))
    if not target:
        return False, f"Application '{app_key}' is not in the allowed launch list."

    try:
        if os.name == "nt":
            os.startfile(target)
        else:
            subprocess.Popen([target])
        _logger.info("Launched application: %s", display_name)
        return True, f"Opened {display_name}."
    except Exception as error:
        _logger.exception("Failed to launch application %s", display_name)
        return False, f"Could not launch {display_name}: {error}"


def handle_system_command(query: str) -> tuple[bool, str]:
    """Parse user query for system actions. Returns (handled, response_text)."""
    text = query.strip().lower()

    # Battery
    if re.search(r"\b(battery|power level|charge status|battery percentage)\b", text):
        return True, get_battery_status()

    # RAM / Memory
    if re.search(r"\b(ram status|ram usage|memory load|system memory|free ram)\b", text):
        return True, get_memory_status()

    # Time / Date
    if re.search(r"\b(what time is it|current time|what is the time|what day is it|current date|what is today's date)\b", text):
        now = datetime.now()
        return True, f"The current time is {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d, %Y')}."

    # Audio & Volume
    if re.search(r"\b(mute audio|mute system|unmute audio|unmute system|mute sound|unmute sound|^mute$|^unmute$)\b", text):
        return True, toggle_mute()

    if re.search(r"\b(volume up|increase volume|turn up the volume|louder)\b", text):
        return True, adjust_volume(increase=True, steps=5)

    if re.search(r"\b(volume down|decrease volume|lower the volume|turn down the volume|softer|quieter)\b", text):
        return True, adjust_volume(increase=False, steps=5)

    # Media Playback
    if re.search(r"\b(play music|pause music|play track|pause track|play/pause|pause playback|resume playback|^pause$|^play$)\b", text):
        return True, control_media("play_pause")

    if re.search(r"\b(next song|next track|skip song|skip track)\b", text):
        return True, control_media("next")

    if re.search(r"\b(previous song|previous track|prev song|prev track)\b", text):
        return True, control_media("previous")

    # Application Launching
    camera_match = re.search(r"\b(open|launch|start|turn on)\s+(the\s+)?(camera|webcam|web cam)\b", text)
    if camera_match:
        success, msg = launch_application("camera")
        return True, msg

    open_match = re.search(r"\b(open|launch|start)\s+(notepad|calculator|spotify|task manager|settings|file explorer|explorer|browser|terminal|vscode|vs code|code|camera)\b", text)
    if open_match:
        app_name = open_match.group(2)
        if app_name in ("vs code", "code"):
            app_name = "vscode"
        elif app_name == "explorer":
            app_name = "file explorer"
        success, msg = launch_application(app_name)
        return True, msg

    return False, ""
