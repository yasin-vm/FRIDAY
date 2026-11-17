"""Loopback control channel for showing or stopping the background assistant."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import subprocess
import threading
from pathlib import Path
from queue import Queue


_logger = logging.getLogger("friday.gui")
_HOST = "127.0.0.1"
_COMMANDS = {"SHOW": "show", "SHUTDOWN": "shutdown"}


class AssistantControlError(RuntimeError):
    """Raised when the running assistant cannot be controlled."""


def _default_port() -> int:
    project_path = str(Path(__file__).resolve().parent).lower().encode("utf-8")
    port_hash = hashlib.sha256(project_path).digest()
    return 40000 + int.from_bytes(port_hash[:2], "big") % 10000


def _friday_process_state() -> str:
    """Return running, absent, or unknown after checking the actual command line."""
    if os.name != "nt":
        return "unknown"

    script_path = str(Path(__file__).resolve().with_name("friday.py"))
    escaped_path = script_path.replace("'", "''")
    command = (
        "$scriptPath = '" + escaped_path + "'; "
        "$matches = @(Get-CimInstance Win32_Process "
        "-Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -and "
        "$_.CommandLine.IndexOf($scriptPath, "
        "[System.StringComparison]::OrdinalIgnoreCase) -ge 0 } | "
        "Select-Object -ExpandProperty ProcessId); "
        "ConvertTo-Json -InputObject $matches -Compress"
    )
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            check=False,
            timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        _logger.exception("Could not verify whether a FRIDAY process is running.")
        return "unknown"

    if result.returncode:
        _logger.warning("Windows process query failed with exit code %d.", result.returncode)
        return "unknown"
    try:
        process_ids = json.loads(result.stdout.decode("utf-8-sig").strip() or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        _logger.exception("Windows process query returned invalid data.")
        return "unknown"
    return "running" if process_ids else "absent"


def _connection_error(port: int, error: OSError) -> AssistantControlError:
    process_state = _friday_process_state()
    endpoint = f"{_HOST}:{port}"
    if process_state == "absent":
        details = (
            f"No FRIDAY assistant process is running and no control server is "
            f"listening at {endpoint}."
        )
    elif process_state == "running":
        details = (
            f"A FRIDAY process is running, but its control server is unavailable "
            f"at {endpoint}."
        )
    else:
        details = (
            f"Could not connect to the FRIDAY control server at {endpoint}, and "
            "the process state could not be verified."
        )
    if isinstance(error, TimeoutError):
        details += " The connection attempt timed out."
    else:
        details += f" Connection error: {error}."
    return AssistantControlError(details)


def send_control(command: str, port: int | None = None) -> None:
    if command not in _COMMANDS:
        raise ValueError(f"Unsupported assistant command: {command}")

    control_port = _default_port() if port is None else port
    _logger.info("Sending local FRIDAY control command: %s", command)
    try:
        client = socket.create_connection((_HOST, control_port), timeout=2)
    except OSError as error:
        _logger.warning(
            "FRIDAY control connection failed at %s:%d: %s",
            _HOST,
            control_port,
            error,
        )
        raise _connection_error(control_port, error) from error

    try:
        with client:
            client.settimeout(2)
            client.sendall(command.encode("ascii") + b"\n")
            response = client.recv(16)
    except OSError as error:
        _logger.exception("FRIDAY control server at %s:%d did not respond.", _HOST, control_port)
        raise AssistantControlError(
            f"The FRIDAY control server at {_HOST}:{control_port} accepted a "
            "connection but did not respond. It may be running but unresponsive."
        ) from error

    if response != b"OK":
        raise AssistantControlError(
            f"The service at {_HOST}:{control_port} did not confirm the FRIDAY "
            "control command."
        )
    _logger.info("FRIDAY control command %s was accepted.", command)


class AssistantControlServer:
    """Accept short-lived local commands and forward them to the Tk event queue."""

    def __init__(self, events: Queue[tuple[str, object]], port: int | None = None) -> None:
        self.events = events
        self.port = _default_port() if port is None else port
        self._listener: socket.socket | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start_or_reconnect(self) -> bool:
        """Start the endpoint, or ask its existing owner to show its GUI."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((_HOST, self.port))
            listener.listen()
            listener.settimeout(0.25)
        except OSError as bind_error:
            listener.close()
            try:
                send_control("SHOW", self.port)
            except AssistantControlError as control_error:
                raise AssistantControlError(
                    f"Could not bind FRIDAY's loopback control port {self.port}: "
                    f"{bind_error}. Reconnect failed: {control_error}"
                ) from control_error
            _logger.info("Reconnected to the existing FRIDAY assistant")
            return False

        self.port = listener.getsockname()[1]
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve,
            name="friday-control",
            daemon=True,
        )
        self._thread.start()
        _logger.info("FRIDAY local control endpoint listening on loopback")
        return True

    def close(self) -> None:
        self._stop_event.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                _logger.exception("Could not close the FRIDAY control socket")
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return

        while not self._stop_event.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    _logger.exception("FRIDAY control endpoint stopped unexpectedly")
                return

            with connection:
                try:
                    connection.settimeout(1)
                    with connection.makefile("rb") as reader:
                        command = reader.readline(33).decode("ascii").strip()
                    event = _COMMANDS.get(command)
                    if event is None:
                        connection.sendall(b"ERROR")
                        _logger.warning("Rejected an invalid FRIDAY control command")
                        continue
                    self.events.put((f"control_{event}", None))
                    connection.sendall(b"OK")
                except (OSError, UnicodeDecodeError):
                    _logger.warning("FRIDAY control client disconnected before completion")
