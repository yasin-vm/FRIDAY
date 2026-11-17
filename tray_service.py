"""Windows System Tray and Global Hotkey service for FRIDAY."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

_logger = logging.getLogger("friday.tray")

WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_HOTKEY = 0x0312
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MOD_WIN = 0x0008
MOD_SHIFT = 0x0004
VK_F = 0x46
HOTKEY_ID = 2026

IDM_SHOW = 1001
IDM_MUTE = 1002
IDM_SHUTDOWN = 1003
MF_STRING = 0x0000
MF_CHECKED = 0x0008
MF_UNCHECKED = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002

if os.name == "nt":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = ctypes.c_longlong

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        ]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HICON),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]


class TrayService:
    """Manages the Windows system tray icon and Win+Shift+F global summon hotkey."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_shutdown: Callable[[], None],
        on_toggle_mute: Callable[[], None] | None = None,
        tooltip: str = "FRIDAY - Local AI Assistant",
    ) -> None:
        self.on_show = on_show
        self.on_shutdown = on_shutdown
        self.on_toggle_mute = on_toggle_mute
        self.tooltip = tooltip
        self.is_muted = False
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._stop_event = threading.Event()
        self._wndproc_ref: Any = None

    def start(self) -> bool:
        if os.name != "nt":
            _logger.info("Tray and hotkeys are only supported on Windows.")
            return False

        self._thread = threading.Thread(
            target=self._run_tray_thread,
            name="friday-tray",
            daemon=True,
        )
        self._thread.start()
        return True

    def update_tooltip(self, text: str) -> None:
        self.tooltip = text
        if os.name == "nt" and self._hwnd:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_TIP
            nid.szTip = text[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def close(self) -> None:
        self._stop_event.set()
        if os.name == "nt" and self._hwnd:
            user32.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        self._hwnd = None

    def _run_tray_thread(self) -> None:
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"FridayTray_{os.getpid()}_{id(self)}"

        def wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_HOTKEY:
                if wparam == HOTKEY_ID:
                    _logger.info("Win+Shift+F global hotkey pressed.")
                    try:
                        self.on_show()
                    except Exception:
                        _logger.exception("Error executing on_show from hotkey.")
                return 0

            if msg == WM_TRAYICON:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    try:
                        self.on_show()
                    except Exception:
                        _logger.exception("Error executing on_show from tray click.")
                    return 0
                if event == WM_RBUTTONUP:
                    self._show_context_menu(hwnd)
                    return 0

            if msg == WM_COMMAND:
                cmd_id = wparam & 0xFFFF
                if cmd_id == IDM_SHOW:
                    self.on_show()
                elif cmd_id == IDM_MUTE:
                    if self.on_toggle_mute:
                        self.on_toggle_mute()
                elif cmd_id == IDM_SHUTDOWN:
                    self.on_shutdown()
                return 0

            if msg == 0x0010:  # WM_CLOSE
                user32.DestroyWindow(hwnd)
                return 0

            if msg == 0x0002:  # WM_DESTROY
                user32.PostQuitMessage(0)
                return 0

            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(wnd_proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = class_name

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            _logger.error("Could not register window class for tray service.")
            return

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "FRIDAY Tray Helper",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hinstance,
            None,
        )
        if not hwnd:
            user32.UnregisterClassW(class_name, hinstance)
            _logger.error("Could not create window for tray service.")
            return

        self._hwnd = hwnd

        # Register Global Hotkey Win+Shift+F (or Alt+Shift+F fallback)
        registered = user32.RegisterHotKey(
            hwnd,
            HOTKEY_ID,
            MOD_WIN | MOD_SHIFT,
            VK_F,
        )
        if not registered:
            registered = user32.RegisterHotKey(
                hwnd,
                HOTKEY_ID,
                0x0001 | MOD_SHIFT,  # MOD_ALT | MOD_SHIFT
                VK_F,
            )
        if registered:
            _logger.info("Registered global summon hotkey successfully.")
        else:
            _logger.info("Global hotkey was already claimed or unavailable in this session.")

        # Add Tray Icon
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))  # IDI_APPLICATION
        nid.szTip = self.tooltip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        # Windows Message Loop
        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            has_msg = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if has_msg in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        user32.UnregisterHotKey(hwnd, HOTKEY_ID)
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, hinstance)
        self._hwnd = None

    def _show_context_menu(self, hwnd: int) -> None:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, IDM_SHOW, "Open FRIDAY (Win+Shift+F)")
        mute_state = MF_CHECKED if self.is_muted else MF_UNCHECKED
        user32.AppendMenuW(
            hmenu,
            MF_STRING | mute_state,
            IDM_MUTE,
            "Mute Wake-Word" if not self.is_muted else "✓ Wake-Word Muted",
        )
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, IDM_SHUTDOWN, "Shutdown FRIDAY")

        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(
            hmenu,
            TPM_RIGHTBUTTON,
            point.x,
            point.y,
            0,
            hwnd,
            None,
        )
        user32.DestroyMenu(hmenu)
