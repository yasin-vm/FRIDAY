import queue
import socket
import unittest

from assistant_control import (
    AssistantControlError,
    AssistantControlServer,
    send_control,
)
from unittest.mock import patch


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class AssistantControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.server = AssistantControlServer(self.events, _free_port())
        self.assertTrue(self.server.start_or_reconnect())

    def tearDown(self) -> None:
        self.server.close()

    def test_reopening_gui_requests_existing_instance_to_show(self) -> None:
        send_control("SHOW", self.server.port)

        self.assertEqual(self.events.get(timeout=1), ("control_show", None))

    def test_shutdown_is_delivered_to_existing_instance(self) -> None:
        send_control("SHUTDOWN", self.server.port)

        self.assertEqual(self.events.get(timeout=1), ("control_shutdown", None))

    def test_disconnected_or_invalid_client_does_not_stop_listener(self) -> None:
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=1) as client:
            client.sendall(b"INVALID\n")
            self.assertEqual(client.recv(16), b"ERROR")

        send_control("SHOW", self.server.port)

        self.assertEqual(self.events.get(timeout=1), ("control_show", None))

    def test_second_server_reconnects_without_owning_another_listener(self) -> None:
        second_server = AssistantControlServer(queue.Queue(), self.server.port)
        try:
            self.assertFalse(second_server.start_or_reconnect())
            self.assertEqual(self.events.get(timeout=1), ("control_show", None))
        finally:
            second_server.close()

    def test_unavailable_assistant_reports_control_error(self) -> None:
        with self.assertRaises(AssistantControlError):
            send_control("SHUTDOWN", _free_port())

    def test_connection_failure_distinguishes_no_assistant_from_unavailable_server(self) -> None:
        with patch("assistant_control.socket.create_connection", side_effect=ConnectionRefusedError), patch(
            "assistant_control._friday_process_state", return_value="absent"
        ):
            with self.assertRaisesRegex(AssistantControlError, "No FRIDAY assistant process"):
                send_control("SHUTDOWN", _free_port())

        with patch("assistant_control.socket.create_connection", side_effect=ConnectionRefusedError), patch(
            "assistant_control._friday_process_state", return_value="running"
        ):
            with self.assertRaisesRegex(AssistantControlError, "process is running"):
                send_control("SHUTDOWN", _free_port())

    def test_connected_but_unresponsive_control_server_is_distinguished(self) -> None:
        class UnresponsiveClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                pass

            def settimeout(self, _timeout) -> None:
                pass

            def sendall(self, _data) -> None:
                pass

            def recv(self, _size) -> bytes:
                raise TimeoutError("timed out")

        with patch(
            "assistant_control.socket.create_connection",
            return_value=UnresponsiveClient(),
        ):
            with self.assertRaisesRegex(
                AssistantControlError,
                "accepted a connection but did not respond",
            ):
                send_control("SHOW", _free_port())


if __name__ == "__main__":
    unittest.main()
