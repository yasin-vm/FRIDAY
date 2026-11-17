"""Unit tests for Windows startup manager."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import startup_manager


class StartupManagerTests(unittest.TestCase):
    def test_non_windows_platform_returns_false(self) -> None:
        with patch("startup_manager.os.name", "posix"):
            self.assertFalse(startup_manager.is_startup_enabled())
            self.assertFalse(startup_manager.enable_startup())
            self.assertFalse(startup_manager.disable_startup())

    @patch("winreg.OpenKey")
    def test_is_startup_enabled_false_on_missing_key(self, mock_open) -> None:
        mock_open.side_effect = FileNotFoundError()
        self.assertFalse(startup_manager.is_startup_enabled())

    @patch("winreg.SetValueEx")
    @patch("winreg.OpenKey")
    def test_enable_startup_success(self, mock_open, mock_set_value) -> None:
        mock_key = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_key

        success = startup_manager.enable_startup()
        self.assertTrue(success)
        mock_set_value.assert_called_once()

    @patch("winreg.DeleteValue")
    @patch("winreg.OpenKey")
    def test_disable_startup_success(self, mock_open, mock_delete) -> None:
        mock_key = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_key

        success = startup_manager.disable_startup()
        self.assertTrue(success)
        mock_delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
