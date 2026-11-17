"""Unit tests for Windows System Tray and Hotkey service."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from tray_service import TrayService


class TrayServiceTests(unittest.TestCase):
    def test_tray_initialization(self) -> None:
        show_mock = Mock()
        shutdown_mock = Mock()
        mute_mock = Mock()

        tray = TrayService(
            on_show=show_mock,
            on_shutdown=shutdown_mock,
            on_toggle_mute=mute_mock,
            tooltip="FRIDAY Test Tooltip",
        )

        self.assertEqual(tray.tooltip, "FRIDAY Test Tooltip")
        self.assertFalse(tray.is_muted)
        self.assertIsNone(tray._hwnd)

    def test_non_windows_platform_gracefully_skips_start(self) -> None:
        tray = TrayService(Mock(), Mock())
        with patch("tray_service.os.name", "posix"):
            started = tray.start()
            self.assertFalse(started)
            self.assertIsNone(tray._thread)

    def test_update_tooltip_updates_internal_state(self) -> None:
        tray = TrayService(Mock(), Mock(), tooltip="Initial")
        tray.update_tooltip("Updated Status")
        self.assertEqual(tray.tooltip, "Updated Status")

    def test_close_signals_stop_event(self) -> None:
        tray = TrayService(Mock(), Mock())
        tray.close()
        self.assertTrue(tray._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
