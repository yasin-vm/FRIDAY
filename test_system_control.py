"""Unit tests for PC control and system actions."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from system_control import (
    adjust_volume,
    control_media,
    get_battery_status,
    get_memory_status,
    handle_system_command,
    launch_application,
    toggle_mute,
)


class SystemControlTests(unittest.TestCase):
    def test_battery_status_returns_string(self) -> None:
        status = get_battery_status()
        self.assertIsInstance(status, str)
        self.assertTrue(len(status) > 0)

    def test_memory_status_returns_string(self) -> None:
        status = get_memory_status()
        self.assertIsInstance(status, str)
        self.assertTrue(len(status) > 0)

    @patch("system_control._press_key")
    def test_volume_and_mute_actions(self, mock_press) -> None:
        up_res = adjust_volume(increase=True, steps=3)
        self.assertIn("Increased", up_res)
        down_res = adjust_volume(increase=False, steps=3)
        self.assertIn("Decreased", down_res)
        mute_res = toggle_mute()
        self.assertIn("Toggled", mute_res)
        self.assertEqual(mock_press.call_count, 3)

    @patch("system_control._press_key")
    def test_media_controls(self, mock_press) -> None:
        self.assertIn("Play/Pause", control_media("play_pause"))
        self.assertIn("next track", control_media("next"))
        self.assertIn("previous track", control_media("previous"))
        self.assertEqual(mock_press.call_count, 3)

    def test_launch_application_rejects_unlisted_app(self) -> None:
        success, msg = launch_application("harmful_app_xyz")
        self.assertFalse(success)
        self.assertIn("not in the allowed launch list", msg)

    @patch("os.startfile", create=True)
    def test_launch_application_allowed(self, mock_startfile) -> None:
        success, msg = launch_application("calculator")
        self.assertTrue(success)
        self.assertIn("Opened Calculator", msg)
        mock_startfile.assert_called_once_with("calc.exe")

    def test_handle_system_command_routes_correctly(self) -> None:
        handled, msg = handle_system_command("what time is it")
        self.assertTrue(handled)
        self.assertIn("current time is", msg)

        handled, msg = handle_system_command("check battery status")
        self.assertTrue(handled)

        handled, msg = handle_system_command("ram usage")
        self.assertTrue(handled)

        handled, msg = handle_system_command("volume up")
        self.assertTrue(handled)

        handled, msg = handle_system_command("mute audio")
        self.assertTrue(handled)

        handled, msg = handle_system_command("pause music")
        self.assertTrue(handled)

        with patch("system_control.launch_application", return_value=(True, "Opened Camera.")) as mock_launch:
            handled, msg = handle_system_command("turn on the camera of mine")
            self.assertTrue(handled)
            self.assertEqual(msg, "Opened Camera.")
            mock_launch.assert_called_once_with("camera")

        handled, msg = handle_system_command("tell me about quantum computing")
        self.assertFalse(handled)
        self.assertEqual(msg, "")


if __name__ == "__main__":
    unittest.main()
