"""Offline Windows speech recognition and synthesis for FRIDAY."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


_logger = logging.getLogger("friday.gui")


class SpeechServiceError(RuntimeError):
    """Raised when offline speech processing cannot be used."""


class SpeechDeviceError(SpeechServiceError):
    """Raised when the default microphone cannot be opened."""


class SpeechNoInputError(SpeechDeviceError):
    """Raised when no meaningful signal was captured from the microphone."""


class SpeechNoRecognitionError(SpeechServiceError):
    """Raised when audio was captured but speech recognition returned no words."""


class SpeechEngine(Protocol):
    def listen(
        self,
        stop_event: threading.Event,
        on_text: Callable[[str], None],
        on_started: Callable[[], None],
        *,
        auto_stop_after_silence: bool = False,
        on_processing: Callable[[], None] | None = None,
    ) -> None: ...

    def listen_for_wake_word(
        self,
        stop_event: threading.Event,
        on_started: Callable[[], None],
        on_detected: Callable[[], None] | None = None,
    ) -> bool: ...

    def speak(
        self,
        text: str,
        stop_event: threading.Event,
        on_started: Callable[[], None],
    ) -> None: ...


_CAPTURE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$capture = $null
$audio = $null
try {
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $captureSource = @'
using System;
using System.Globalization;
using System.IO;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public sealed class FridayMicrophoneCapture : IDisposable
{
    private const uint WaveMapper = 0xFFFFFFFF;
    private const uint CallbackFunction = 0x00030000;
    private const uint WaveDataMessage = 0x03C0;
    private const int BufferCount = 4;
    private const int BufferSize = 8192;
    private const int SignalThreshold = 327;

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    private struct WaveFormat
    {
        public ushort FormatTag;
        public ushort Channels;
        public uint SamplesPerSecond;
        public uint AverageBytesPerSecond;
        public ushort BlockAlign;
        public ushort BitsPerSample;
        public ushort ExtraSize;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct WaveHeader
    {
        public IntPtr Data;
        public uint BufferLength;
        public uint BytesRecorded;
        public UIntPtr UserData;
        public uint Flags;
        public uint Loops;
        public IntPtr Next;
        public UIntPtr Reserved;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WaveInputCapabilities
    {
        public ushort ManufacturerId;
        public ushort ProductId;
        public uint DriverVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string ProductName;
        public uint Formats;
        public ushort Channels;
        public ushort Reserved;
        public uint Support;
    }

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate void WaveInCallback(
        IntPtr device, uint message, IntPtr instance, IntPtr parameter1, IntPtr parameter2
    );

    [DllImport("winmm.dll", EntryPoint = "waveInOpen")]
    private static extern int WaveInOpen(
        out IntPtr device, uint deviceId, ref WaveFormat format,
        WaveInCallback callback, IntPtr instance, uint flags
    );

    [DllImport("winmm.dll", EntryPoint = "waveInGetID")]
    private static extern int WaveInGetId(IntPtr device, out uint deviceId);

    [DllImport("winmm.dll", EntryPoint = "waveInGetDevCapsW", CharSet = CharSet.Unicode)]
    private static extern int WaveInGetCapabilities(
        uint deviceId, ref WaveInputCapabilities capabilities, uint size
    );

    [DllImport("winmm.dll", EntryPoint = "waveInPrepareHeader")]
    private static extern int WaveInPrepareHeader(IntPtr device, IntPtr header, uint size);

    [DllImport("winmm.dll", EntryPoint = "waveInAddBuffer")]
    private static extern int WaveInAddBuffer(IntPtr device, IntPtr header, uint size);

    [DllImport("winmm.dll", EntryPoint = "waveInStart")]
    private static extern int WaveInStart(IntPtr device);

    [DllImport("winmm.dll", EntryPoint = "waveInStop")]
    private static extern int WaveInStop(IntPtr device);

    [DllImport("winmm.dll", EntryPoint = "waveInReset")]
    private static extern int WaveInReset(IntPtr device);

    [DllImport("winmm.dll", EntryPoint = "waveInUnprepareHeader")]
    private static extern int WaveInUnprepareHeader(IntPtr device, IntPtr header, uint size);

    [DllImport("winmm.dll", EntryPoint = "waveInClose")]
    private static extern int WaveInClose(IntPtr device);

    private readonly object _audioLock = new object();
    private readonly object _stdoutLock = new object();
    private readonly MemoryStream _audio = new MemoryStream();
    private readonly Stream _stdout = Console.OpenStandardOutput();
    private readonly List<IntPtr> _headers = new List<IntPtr>();
    private readonly WaveInCallback _callback;
    private IntPtr _device;
    private volatile bool _capturing;
    private DateTime _lastLevelLog = DateTime.MinValue;
    private int _maximumLevel;
    private bool _signalDetected;
    private bool _retainAudio;

    public int MaximumLevel
    {
        get { return _maximumLevel; }
    }

    public bool SignalDetected
    {
        get { return _signalDetected; }
    }

    public FridayMicrophoneCapture()
    {
        _callback = OnWaveData;
    }

    public void Start(bool retainAudio)
    {
        _retainAudio = retainAudio;
        WaveFormat format = new WaveFormat();
        format.FormatTag = 1;
        format.Channels = 1;
        format.SamplesPerSecond = 16000;
        format.BitsPerSample = 16;
        format.BlockAlign = 2;
        format.AverageBytesPerSecond = 32000;
        int result = WaveInOpen(
            out _device, WaveMapper, ref format, _callback, IntPtr.Zero, CallbackFunction
        );
        Check(result, "opening the Windows default microphone");
        uint actualDeviceId;
        if (WaveInGetId(_device, out actualDeviceId) == 0)
        {
            WaveInputCapabilities capabilities = new WaveInputCapabilities();
            if (WaveInGetCapabilities(
                actualDeviceId, ref capabilities, (uint)Marshal.SizeOf(typeof(WaveInputCapabilities))
            ) == 0)
            {
                Console.Error.WriteLine(
                    "DEVICE:WinMM default mapped to " + capabilities.ProductName.Trim()
                    + " (device " + actualDeviceId + ")"
                );
                Console.Error.Flush();
            }
        }

        try
        {
            for (int index = 0; index < BufferCount; index++)
            {
                IntPtr data = Marshal.AllocHGlobal(BufferSize);
                IntPtr header = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(WaveHeader)));
                WaveHeader waveHeader = new WaveHeader();
                waveHeader.Data = data;
                waveHeader.BufferLength = BufferSize;
                Marshal.StructureToPtr(waveHeader, header, false);
                _headers.Add(header);
                Check(WaveInPrepareHeader(_device, header, (uint)Marshal.SizeOf(typeof(WaveHeader))),
                    "preparing a microphone buffer");
                Check(WaveInAddBuffer(_device, header, (uint)Marshal.SizeOf(typeof(WaveHeader))),
                    "starting a microphone buffer");
            }
            _capturing = true;
            Check(WaveInStart(_device), "starting microphone capture");
            Console.Error.WriteLine("CAPTURE_STARTED:16000Hz:mono:16bit:RAM");
            Console.Error.Flush();
        }
        catch
        {
            StopDevice();
            throw;
        }
    }

    private void OnWaveData(
        IntPtr device, uint message, IntPtr instance, IntPtr parameter1, IntPtr parameter2
    )
    {
        if (message != WaveDataMessage || parameter1 == IntPtr.Zero)
            return;
        try
        {
            WaveHeader header = (WaveHeader)Marshal.PtrToStructure(parameter1, typeof(WaveHeader));
            int count = (int)header.BytesRecorded;
            if (count > 0)
            {
                byte[] bytes = new byte[count];
                Marshal.Copy(header.Data, bytes, 0, count);
                int peak = 0;
                for (int offset = 0; offset + 1 < count; offset += 2)
                {
                    int sample = Math.Abs((int)BitConverter.ToInt16(bytes, offset));
                    if (sample > peak)
                        peak = sample;
                }
                if (_retainAudio)
                {
                    lock (_audioLock)
                    {
                        _audio.Write(bytes, 0, bytes.Length);
                    }
                }
                lock (_stdoutLock)
                {
                    _stdout.Write(bytes, 0, bytes.Length);
                    _stdout.Flush();
                }
                Array.Clear(bytes, 0, bytes.Length);
                if (peak > _maximumLevel)
                    _maximumLevel = peak;
                if (!_signalDetected && peak >= SignalThreshold)
                {
                    _signalDetected = true;
                    Console.Error.WriteLine("AUDIO_INPUT:DETECTED");
                    Console.Error.Flush();
                }
                DateTime now = DateTime.UtcNow;
                if ((now - _lastLevelLog).TotalSeconds >= 1)
                {
                    _lastLevelLog = now;
                    Console.Error.WriteLine(
                        "LEVEL:" + (peak * 100 / 32768).ToString(CultureInfo.InvariantCulture)
                    );
                    Console.Error.Flush();
                }
            }
            if (_capturing)
            {
                header.BytesRecorded = 0;
                Marshal.StructureToPtr(header, parameter1, false);
                int result = WaveInAddBuffer(device, parameter1,
                    (uint)Marshal.SizeOf(typeof(WaveHeader)));
                if (result != 0)
                    Console.Error.WriteLine("Could not requeue microphone buffer; MMRESULT " + result);
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Microphone callback failed: " + error);
        }
    }

    public byte[] StopToPcmBytes()
    {
        StopDevice();
        byte[] audio;
        lock (_audioLock)
        {
            audio = _audio.ToArray();
            Array.Clear(_audio.GetBuffer(), 0, _audio.Capacity);
            _audio.SetLength(0);
        }
        Console.Error.WriteLine("CAPTURE_BYTES:" + audio.Length);
        Console.Error.WriteLine("MAX_LEVEL:" + (_maximumLevel * 100 / 32768));
        Console.Error.Flush();
        if (!_signalDetected)
        {
            Console.Error.WriteLine("NO_AUDIO_INPUT");
            Console.Error.Flush();
        }
        return audio;
    }

    private void StopDevice()
    {
        _capturing = false;
        if (_device == IntPtr.Zero)
            return;
        WaveInStop(_device);
        WaveInReset(_device);
        int headerSize = Marshal.SizeOf(typeof(WaveHeader));
        foreach (IntPtr header in _headers)
        {
            WaveInUnprepareHeader(_device, header, (uint)headerSize);
            WaveHeader waveHeader = (WaveHeader)Marshal.PtrToStructure(header, typeof(WaveHeader));
            Marshal.FreeHGlobal(waveHeader.Data);
            Marshal.DestroyStructure(header, typeof(WaveHeader));
            Marshal.FreeHGlobal(header);
        }
        _headers.Clear();
        WaveInClose(_device);
        _device = IntPtr.Zero;
    }

    private static void Check(int result, string action)
    {
        if (result != 0)
            throw new InvalidOperationException(
                "Windows failed while " + action + " (MMRESULT " + result + ")."
            );
    }

    public void Dispose()
    {
        StopDevice();
        lock (_audioLock)
        {
            Array.Clear(_audio.GetBuffer(), 0, (int)_audio.Length);
            _audio.SetLength(0);
        }
        _audio.Dispose();
    }
}

'@
    Add-Type -TypeDefinition $captureSource | Out-Null
    $mode = [Console]::In.ReadLine()
    if ($null -eq $mode) { throw 'Microphone capture mode was not provided.' }
    $capture = New-Object FridayMicrophoneCapture
    $capture.Start($mode -ne 'WAKE')
    [Console]::Error.WriteLine('READY')
    [Console]::Error.Flush()
    while (($command = [Console]::In.ReadLine()) -ne $null) {
        if ($command -eq 'STOP') { break }
    }
    [Console]::Error.WriteLine('STOPPING')
    [Console]::Error.Flush()
    $audio = $capture.StopToPcmBytes()
} catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
    exit 1
} finally {
    if ($null -ne $audio) {
        [Array]::Clear($audio, 0, $audio.Length)
    }
    if ($null -ne $capture) {
        $capture.Dispose()
    }
}
"""

_SYNTHESIS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    Add-Type -AssemblyName System.Speech
    $text = [Console]::In.ReadToEnd()
    $synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synthesizer.Speak($text)
    $synthesizer.Dispose()
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""

_CODE_BLOCK = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`+[^`]*(?:`+|$)")
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MARKDOWN_DECORATION = re.compile(r"(?m)^\s{0,3}#{1,6}\s*|[*_~>|]")


class _SilenceStopDetector:
    def __init__(self, threshold: int, silence_seconds: float) -> None:
        self.threshold = threshold
        self.silence_seconds = silence_seconds
        self.last_signal_at: float | None = None

    def observe(self, pcm: bytearray, now: float) -> bool:
        peak = max(
            (
                abs(int.from_bytes(pcm[offset : offset + 2], "little", signed=True))
                for offset in range(0, len(pcm) - 1, 2)
            ),
            default=0,
        )
        if peak >= self.threshold:
            self.last_signal_at = now
            return False
        return (
            self.last_signal_at is not None
            and now - self.last_signal_at >= self.silence_seconds
        )


def _wake_phrase_from_result(result_json: str) -> bool:
    result = json.loads(result_json)
    text = result.get("partial", result.get("text", ""))
    words = " ".join(re.findall(r"[a-z]+", text.lower()))
    return words == FasterWhisperEngine._WAKE_PHRASE


def prepare_speech_text(text: str) -> str:
    """Remove code and Markdown syntax that should not be spoken aloud."""
    text = _CODE_BLOCK.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _MARKDOWN_DECORATION.sub(" ", text)
    text = re.sub(r"(?m)^\s*[-+]\s+", "", text)
    text = re.sub(r"[^\w\s.,!?;:'’()-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


class WindowsSpeechEngine:
    """Run Windows offline text-to-speech without affecting Whisper input."""

    def __init__(self) -> None:
        self._process_lock = threading.Lock()
        self._speech_process: subprocess.Popen[str] | None = None

    def _powershell(self) -> str:
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if executable is None:
            raise SpeechServiceError(
                "Windows PowerShell is unavailable; offline speech cannot start."
            )
        return executable

    def stop_all(self) -> None:
        with self._process_lock:
            process = self._speech_process
        if process is not None and process.poll() is None:
            process.terminate()

    def _start_process(
        self, script: str, *, stdout: int | None, stderr: int | None
    ) -> subprocess.Popen[str]:
        try:
            return subprocess.Popen(
                [
                    self._powershell(),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            raise SpeechServiceError(f"Could not start Windows speech: {error}") from error

    def speak(
        self,
        text: str,
        stop_event: threading.Event,
        on_started: Callable[[], None],
    ) -> None:
        process = self._start_process(
            _SYNTHESIS_SCRIPT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with self._process_lock:
            self._speech_process = process
        try:
            if process.stdin is None:
                raise SpeechServiceError("Could not send text to Windows speech.")
            process.stdin.write(text)
            process.stdin.close()
            process.stdin = None
            on_started()
            while process.poll() is None:
                if stop_event.wait(0.05):
                    process.terminate()
                    break
            _, error_output = process.communicate(timeout=2)
            if process.returncode and not stop_event.is_set():
                details = (error_output or "").strip()
                raise SpeechServiceError(
                    details or "Windows speech synthesis stopped unexpectedly."
                )
        except (OSError, subprocess.SubprocessError) as error:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
            if not stop_event.is_set():
                raise SpeechServiceError(f"Could not speak the response: {error}") from error
        finally:
            with self._process_lock:
                if self._speech_process is process:
                    self._speech_process = None
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
            if process.stderr is not None:
                process.stderr.close()


class FasterWhisperEngine:
    """Capture microphone PCM in memory and transcribe it offline with Whisper."""

    _MODEL_NAME = "small.en"
    _BEAM_SIZE = 5
    _NO_SPEECH_THRESHOLD = 0.6
    _LOG_PROB_THRESHOLD = -1.0
    _SAMPLE_RATE = 16000
    _VAD_THRESHOLD = 0.35
    _WAKE_MODEL_NAME = "vosk-model-small-en-us-0.15"
    _WAKE_PHRASE_GRAMMAR = '["hey friday"]'
    _WAKE_PHRASE = "hey friday"
    _AUTO_STOP_SILENCE_SECONDS = 1.5
    _AUTO_STOP_SIGNAL_THRESHOLD = 327

    def __init__(self, model_directory: Path | None = None) -> None:
        self.model_directory = model_directory or (
            Path(__file__).resolve().parent / "models" / "speech"
        )
        self._model_lock = threading.Lock()
        self._model = None
        self._model_backend: str | None = None
        self._wake_model_lock = threading.Lock()
        self._wake_model = None
        self._capture_lock = threading.Lock()
        self._capture_process: subprocess.Popen[bytes] | None = None
        self._synthesizer = WindowsSpeechEngine()
        self._closed = False

    def _create_model(self, device: str, compute_type: str):
        from faster_whisper import WhisperModel

        return WhisperModel(
            str(self.model_directory),
            device=device,
            compute_type=compute_type,
            cpu_threads=max(1, (os.cpu_count() or 2) - 1) if device == "cpu" else 0,
        )

    def _get_model(self):
        with self._model_lock:
            if self._closed:
                raise SpeechServiceError("Offline speech recognition has been closed.")
            if self._model is not None:
                return self._model, self._model_backend
            if not (self.model_directory / "model.bin").is_file():
                raise SpeechServiceError(
                    f"The offline {self._MODEL_NAME} model is missing from "
                    f"{self.model_directory}."
                )

            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() < 1:
                    raise RuntimeError("CTranslate2 cannot access a CUDA device.")
                self._model = self._create_model("cuda", "int8_float16")
                self._model_backend = "CUDA"
            except Exception as gpu_error:
                _logger.warning(
                    "Could not initialize faster-whisper CUDA; using CPU int8 instead: %s",
                    gpu_error,
                )
                try:
                    self._model = self._create_model("cpu", "int8")
                    self._model_backend = "CPU"
                except Exception as cpu_error:
                    self._model = None
                    raise SpeechServiceError(
                        f"Could not initialize offline Whisper on CUDA or CPU: {cpu_error}"
                    ) from cpu_error

            _logger.info(
                "Loaded faster-whisper %s model using %s.",
                self._MODEL_NAME,
                self._model_backend,
            )
            if self._closed:
                self._model = None
                self._model_backend = None
                raise SpeechServiceError("Offline speech recognition has been closed.")
            return self._model, self._model_backend

    def _start_capture(self, *, wake_word_mode: bool = False) -> subprocess.Popen[bytes]:
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if executable is None:
            raise SpeechDeviceError(
                "Windows PowerShell is unavailable; the microphone cannot be opened."
            )
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _CAPTURE_SCRIPT,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.stdin is None:
                raise SpeechDeviceError("Could not configure the Windows microphone capture.")
            process.stdin.write(b"WAKE\n" if wake_word_mode else b"CAPTURE\n")
            process.stdin.flush()
        except OSError as error:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            raise SpeechDeviceError(
                f"Could not start in-memory microphone capture: {error}"
            ) from error
        except SpeechDeviceError:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            raise
        with self._capture_lock:
            self._capture_process = process
        return process

    def _capture_pcm(
        self,
        stop_event: threading.Event,
        on_started: Callable[[], None],
        *,
        on_chunk: Callable[[bytearray], None] | None = None,
        auto_stop_after_silence: bool = False,
    ) -> tuple[bytearray, int]:
        process = self._start_capture(wake_word_mode=on_chunk is not None)
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        audio_queue: queue.Queue[bytearray | None] = queue.Queue(maxsize=8)
        diagnostic_queue: queue.Queue[str | None] = queue.Queue(maxsize=64)
        reader_errors: list[str] = []

        def read_audio() -> None:
            try:
                while True:
                    chunk = process.stdout.read(8192)
                    if not chunk:
                        break
                    audio_queue.put(bytearray(chunk))
                    del chunk
            except OSError as error:
                reader_errors.append(str(error))
            finally:
                audio_queue.put(None)

        def read_diagnostics() -> None:
            try:
                for raw_line in process.stderr:
                    diagnostic_queue.put(raw_line.decode("utf-8", errors="replace").strip())
            except OSError as error:
                reader_errors.append(str(error))
            finally:
                diagnostic_queue.put(None)

        audio_reader = threading.Thread(target=read_audio, daemon=True)
        diagnostic_reader = threading.Thread(target=read_diagnostics, daemon=True)
        audio_reader.start()
        diagnostic_reader.start()

        pcm = bytearray()
        device_ready = False
        audio_done = False
        diagnostics_done = False
        stop_sent = False
        stop_deadline: float | None = None
        saw_input = False
        peak_level = 0
        captured_bytes = 0
        odd_pcm_byte: int | None = None
        silence_detector = (
            _SilenceStopDetector(
                self._AUTO_STOP_SIGNAL_THRESHOLD,
                self._AUTO_STOP_SILENCE_SECONDS,
            )
            if auto_stop_after_silence
            else None
        )
        capture_started_at: float | None = None
        diagnostics: list[str] = []
        try:
            _logger.info(
                "Starting in-memory microphone capture for %s.",
                "wake-word detection" if on_chunk is not None else "faster-whisper",
            )
            while not audio_done or not diagnostics_done or process.poll() is None:
                if stop_event.is_set() and not stop_sent:
                    stop_sent = True
                    stop_deadline = time.monotonic() + 30
                    _logger.info("Stopping microphone capture and collecting RAM audio.")
                    try:
                        process.stdin.write(b"STOP\n")
                        process.stdin.flush()
                    except OSError as error:
                        diagnostics.append(str(error))

                if (
                    stop_deadline is not None
                    and process.poll() is None
                    and time.monotonic() >= stop_deadline
                ):
                    _logger.error("Microphone capture exceeded its stop timeout.")
                    process.terminate()

                if (
                    on_chunk is not None
                    and device_ready
                    and not saw_input
                    and not stop_event.is_set()
                    and capture_started_at is not None
                    and time.monotonic() - capture_started_at >= 3
                ):
                    raise SpeechNoInputError(
                        "No microphone input was detected while wake-word listening. "
                        "Check the Windows default recording device and microphone "
                        "privacy permissions."
                    )

                try:
                    line = diagnostic_queue.get(timeout=0.05)
                    if line is None:
                        diagnostics_done = True
                    elif line == "READY":
                        device_ready = True
                        capture_started_at = time.monotonic()
                        if on_chunk is not None:
                            _logger.info(
                                "Microphone capture is active for Vosk wake-word detection."
                            )
                        else:
                            _logger.info("Windows microphone capture is ready.")
                        on_started()
                    elif line.startswith("DEVICE:"):
                        _logger.info("Speech input device: %s", line[7:])
                    elif line.startswith("CAPTURE_STARTED:"):
                        _logger.info("Microphone capture started: %s", line[16:])
                    elif line.startswith("LEVEL:"):
                        _logger.info("In-memory microphone input level: %s%%", line[6:])
                    elif line == "AUDIO_INPUT:DETECTED":
                        saw_input = True
                        _logger.info("Non-silent microphone input detected.")
                    elif line.startswith("CAPTURE_BYTES:"):
                        _logger.info("Captured audio kept in RAM: %s bytes.", line[14:])
                    elif line.startswith("MAX_LEVEL:"):
                        peak_level = int(line[10:])
                        _logger.info("Peak microphone input level: %s%%.", peak_level)
                    elif line == "NO_AUDIO_INPUT":
                        _logger.warning("No microphone signal was detected.")
                    elif line == "STOPPING":
                        _logger.debug("Microphone capture stopped.")
                    else:
                        diagnostics.append(line)
                except queue.Empty:
                    pass

                try:
                    audio_chunk = audio_queue.get_nowait()
                    if audio_chunk is None:
                        audio_done = True
                    else:
                        if odd_pcm_byte is not None:
                            audio_chunk.insert(0, odd_pcm_byte)
                            odd_pcm_byte = None
                        if len(audio_chunk) % 2:
                            odd_pcm_byte = audio_chunk.pop()
                        if audio_chunk:
                            if on_chunk is not None:
                                on_chunk(audio_chunk)
                            if on_chunk is None:
                                pcm.extend(audio_chunk)
                            captured_bytes += len(audio_chunk)
                            if silence_detector is not None:
                                if silence_detector.observe(audio_chunk, time.monotonic()):
                                    _logger.info(
                                        "Automatically stopping speech capture after "
                                        "%.1f seconds of silence.",
                                        self._AUTO_STOP_SILENCE_SECONDS,
                                    )
                                    stop_event.set()
                        audio_chunk[:] = b"\x00" * len(audio_chunk)
                        del audio_chunk
                except queue.Empty:
                    pass

            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=2)
            audio_reader.join(timeout=1)
            diagnostic_reader.join(timeout=1)
            while True:
                try:
                    audio_chunk = audio_queue.get_nowait()
                except queue.Empty:
                    break
                if audio_chunk is not None:
                    audio_chunk[:] = b"\x00" * len(audio_chunk)

            diagnostic_text = "\n".join(
                item for item in (*diagnostics, *reader_errors) if item
            )
            if reader_errors:
                raise SpeechServiceError(
                    f"Could not receive captured microphone audio: {diagnostic_text}"
                )
            if process.returncode:
                if not device_ready:
                    raise SpeechDeviceError(
                        diagnostic_text
                        or "Could not open the Windows default microphone."
                    )
                raise SpeechDeviceError(
                    diagnostic_text or "Microphone capture stopped unexpectedly."
                )
            if not device_ready:
                raise SpeechDeviceError(
                    diagnostic_text or "The Windows default microphone did not start."
                )
            if on_chunk is not None and stop_event.is_set() and not saw_input:
                return pcm, peak_level
            if not saw_input or peak_level == 0 or (on_chunk is None and not pcm):
                raise SpeechNoInputError(
                    "No microphone input was detected. Check the Windows default "
                    "recording device, microphone mute, and privacy permissions."
                )
            if len(pcm) % 2:
                _logger.warning("Discarding one trailing byte from captured PCM.")
                pcm.pop()
            duration_seconds = captured_bytes / (self._SAMPLE_RATE * 2)
            _logger.info(
                "Captured PCM format: signed 16-bit little-endian, mono, %d Hz; "
                "bytes=%d, duration=%.2fs.",
                self._SAMPLE_RATE,
                captured_bytes + (1 if odd_pcm_byte is not None else 0),
                duration_seconds,
            )
            return pcm, peak_level
        except Exception:
            pcm[:] = b"\x00" * len(pcm)
            raise
        finally:
            with self._capture_lock:
                if self._capture_process is process:
                    self._capture_process = None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            audio_reader.join(timeout=1)
            diagnostic_reader.join(timeout=1)

    def _recognize(self, model, waveform) -> str:
        segments, _ = model.transcribe(
            waveform,
            language="en",
            beam_size=self._BEAM_SIZE,
            vad_filter=True,
            vad_parameters={
                "threshold": self._VAD_THRESHOLD,
                "min_speech_duration_ms": 100,
                "min_silence_duration_ms": 400,
                "speech_pad_ms": 500,
            },
            no_speech_threshold=self._NO_SPEECH_THRESHOLD,
            log_prob_threshold=self._LOG_PROB_THRESHOLD,
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        accepted: list[str] = []
        segment_count = 0
        for segment in segments:
            segment_count += 1
            no_speech_probability = getattr(segment, "no_speech_prob", 0.0)
            average_log_probability = getattr(segment, "avg_logprob", 0.0)
            text = segment.text.strip()
            _logger.info(
                "Whisper segment diagnostics: duration=%.2fs, chars=%d, "
                "no_speech=%.3f, avg_logprob=%.3f, decoder_accepted=true.",
                max(0.0, getattr(segment, "end", 0.0) - getattr(segment, "start", 0.0)),
                len(text),
                no_speech_probability,
                average_log_probability,
            )
            if text:
                accepted.append(text)
        if not segment_count:
            _logger.info(
                "Whisper emitted no speech segments (VAD threshold=%.2f, "
                "minimum speech=100ms).",
                self._VAD_THRESHOLD,
            )
        return " ".join(accepted).strip()

    def _transcribe(self, pcm: bytearray) -> str:
        import numpy as np

        if not pcm:
            raise SpeechNoInputError("No audio was captured from the microphone.")
        waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        waveform *= 1.0 / 32768.0
        duration_seconds = waveform.size / self._SAMPLE_RATE
        peak_amplitude = float(np.max(np.abs(waveform))) if waveform.size else 0.0
        rms_amplitude = float(np.sqrt(np.mean(np.square(waveform)))) if waveform.size else 0.0
        clipped_samples = int(np.count_nonzero(np.abs(waveform) >= (32760 / 32768)))
        _logger.info(
            "Whisper waveform diagnostics: samples=%d, duration=%.2fs, "
            "peak=%.4f, rms=%.4f, clipped_samples=%d.",
            waveform.size,
            duration_seconds,
            peak_amplitude,
            rms_amplitude,
            clipped_samples,
        )
        pcm[:] = b"\x00" * len(pcm)
        try:
            model, backend = self._get_model()
            try:
                transcript = self._recognize(model, waveform)
            except Exception as gpu_error:
                if backend != "CUDA":
                    raise
                _logger.warning(
                    "faster-whisper CUDA inference failed; retrying with CPU int8: %s",
                    gpu_error,
                )
                with self._model_lock:
                    self._model = self._create_model("cpu", "int8")
                    self._model_backend = "CPU"
                    model = self._model
                _logger.info("Loaded faster-whisper %s model using CPU.", self._MODEL_NAME)
                transcript = self._recognize(model, waveform)
            if not transcript:
                raise SpeechNoRecognitionError(
                    "Microphone audio was captured, but Whisper found no reliable "
                    "English speech. Try speaking closer to the microphone."
                )
            return transcript
        except SpeechServiceError:
            raise
        except Exception as error:
            raise SpeechServiceError(
                f"Offline faster-whisper transcription failed: {error}"
            ) from error
        finally:
            waveform.fill(0)
            pcm[:] = b"\x00" * len(pcm)

    def listen(
        self,
        stop_event: threading.Event,
        on_text: Callable[[str], None],
        on_started: Callable[[], None],
        *,
        auto_stop_after_silence: bool = False,
        on_processing: Callable[[], None] | None = None,
    ) -> None:
        pcm = bytearray()
        try:
            pcm, peak_level = self._capture_pcm(
                stop_event,
                on_started,
                auto_stop_after_silence=auto_stop_after_silence,
            )
            _logger.info(
                "Transcribing %d bytes of in-memory audio (peak=%d%%).",
                len(pcm),
                peak_level,
            )
            if on_processing is not None:
                on_processing()
            transcript = self._transcribe(pcm)
            _logger.info(
                "faster-whisper returned recognized text (characters=%d).",
                len(transcript),
            )
            on_text(transcript)
        finally:
            pcm[:] = b"\x00" * len(pcm)

    def _get_wake_model(self):
        with self._wake_model_lock:
            if self._closed:
                raise SpeechServiceError("Offline wake-word detection has been closed.")
            if self._wake_model is not None:
                return self._wake_model

            model_path = self.model_directory / self._WAKE_MODEL_NAME
            if not (model_path / "am" / "final.mdl").is_file():
                raise SpeechServiceError(
                    f"The offline wake-word model is missing from {model_path}."
                )
            try:
                from vosk import Model, SetLogLevel

                SetLogLevel(-1)
                self._wake_model = Model(str(model_path))
            except Exception as error:
                raise SpeechServiceError(
                    f"Could not initialize the offline wake-word model: {error}"
                ) from error
            _logger.info("Vosk wake-word model initialized successfully.")
            return self._wake_model

    def listen_for_wake_word(
        self,
        stop_event: threading.Event,
        on_started: Callable[[], None],
        on_detected: Callable[[], None] | None = None,
    ) -> bool:
        try:
            from vosk import KaldiRecognizer
        except ImportError as error:
            raise SpeechServiceError(
                "Vosk is required for offline wake-word detection."
            ) from error

        model = self._get_wake_model()
        recognizer = KaldiRecognizer(
            model,
            self._SAMPLE_RATE,
            self._WAKE_PHRASE_GRAMMAR,
        )
        _logger.info("Vosk recognizer initialized for the Hey FRIDAY grammar.")
        detected = False

        def process_chunk(chunk: bytearray) -> None:
            nonlocal detected
            if detected or stop_event.is_set():
                return
            finalized = recognizer.AcceptWaveform(bytes(chunk))
            result_json = recognizer.Result() if finalized else recognizer.PartialResult()
            if _wake_phrase_from_result(result_json):
                detected = True
                _logger.info("Wake phrase detected by the local recognizer.")
                if on_detected is not None:
                    on_detected()
                stop_event.set()

        pcm = bytearray()
        try:
            pcm, _ = self._capture_pcm(
                stop_event,
                on_started,
                on_chunk=process_chunk,
            )
        finally:
            pcm[:] = b"\x00" * len(pcm)
            del recognizer
        return detected

    def speak(
        self,
        text: str,
        stop_event: threading.Event,
        on_started: Callable[[], None],
    ) -> None:
        self._synthesizer.speak(text, stop_event, on_started)

    def stop_all(self) -> None:
        self._closed = True
        with self._capture_lock:
            process = self._capture_process
        if process is not None and process.poll() is None:
            process.terminate()
        self._model = None
        self._wake_model = None
        self._synthesizer.stop_all()


class SpeechService:
    """Run offline recognition and cancellable speech away from Tk's thread."""

    def __init__(self, engine: SpeechEngine | None = None) -> None:
        self.engine = engine or FasterWhisperEngine()
        self._lock = threading.Lock()
        self._recording_thread: threading.Thread | None = None
        self._recording_stop: threading.Event | None = None
        self._wake_thread: threading.Thread | None = None
        self._wake_stop: threading.Event | None = None
        self._speaking_thread: threading.Thread | None = None
        self._speaking_stop: threading.Event | None = None

    def start_listening(
        self,
        on_text: Callable[[str], None],
        on_started: Callable[[], None],
        on_finished: Callable[[], None],
        on_error: Callable[[Exception], None],
        *,
        auto_stop_after_silence: bool = False,
        on_processing: Callable[[], None] | None = None,
    ) -> bool:
        with self._lock:
            if self._recording_thread is not None and self._recording_thread.is_alive():
                return False
            if self._wake_thread is not None and self._wake_thread.is_alive():
                return False
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._listen_worker,
                args=(
                    stop_event,
                    on_text,
                    on_started,
                    on_finished,
                    on_error,
                    auto_stop_after_silence,
                    on_processing,
                ),
                name="friday-speech-recognition",
                daemon=True,
            )
            self._recording_stop = stop_event
            self._recording_thread = thread
            thread.start()
            return True

    def _listen_worker(
        self,
        stop_event: threading.Event,
        on_text: Callable[[str], None],
        on_started: Callable[[], None],
        on_finished: Callable[[], None],
        on_error: Callable[[Exception], None],
        auto_stop_after_silence: bool,
        on_processing: Callable[[], None] | None,
    ) -> None:
        try:
            if auto_stop_after_silence:
                self.engine.listen(
                    stop_event,
                    on_text,
                    on_started,
                    auto_stop_after_silence=True,
                    on_processing=on_processing,
                )
            elif on_processing is not None:
                self.engine.listen(
                    stop_event,
                    on_text,
                    on_started,
                    on_processing=on_processing,
                )
            else:
                self.engine.listen(stop_event, on_text, on_started)
        except Exception as error:
            _logger.exception("Speech input worker failed.")
            on_error(error)
        finally:
            with self._lock:
                if self._recording_stop is stop_event:
                    self._recording_stop = None
                    self._recording_thread = None
            on_finished()

    def start_wake_word(
        self,
        on_finished: Callable[[bool], None],
        on_started: Callable[[], None],
        on_error: Callable[[Exception], None],
        on_detected: Callable[[], None] | None = None,
    ) -> bool:
        listen_for_wake_word = getattr(self.engine, "listen_for_wake_word", None)
        if not callable(listen_for_wake_word):
            on_error(SpeechServiceError("Offline wake-word detection is unavailable."))
            return False
        with self._lock:
            if (
                (self._recording_thread is not None and self._recording_thread.is_alive())
                or (self._speaking_thread is not None and self._speaking_thread.is_alive())
                or (self._wake_thread is not None and self._wake_thread.is_alive())
            ):
                return False
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._wake_worker,
                args=(stop_event, on_started, on_detected, on_finished, on_error),
                name="friday-wake-word-detection",
                daemon=True,
            )
            self._wake_stop = stop_event
            self._wake_thread = thread
            thread.start()
            return True

    def _wake_worker(
        self,
        stop_event: threading.Event,
        on_started: Callable[[], None],
        on_detected: Callable[[], None] | None,
        on_finished: Callable[[bool], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        detected = False
        try:
            listen_for_wake_word = getattr(self.engine, "listen_for_wake_word")
            if on_detected is None:
                detected = bool(listen_for_wake_word(stop_event, on_started))
            else:
                detected = bool(
                    listen_for_wake_word(stop_event, on_started, on_detected)
                )
        except Exception as error:
            _logger.exception("Wake-word detection worker failed.")
            on_error(error)
        finally:
            with self._lock:
                if self._wake_stop is stop_event:
                    self._wake_stop = None
                    self._wake_thread = None
            on_finished(detected)

    def stop_wake_word(self) -> bool:
        with self._lock:
            stop_event = self._wake_stop
        if stop_event is None:
            return False
        stop_event.set()
        return True

    def stop_listening(self) -> bool:
        with self._lock:
            stop_event = self._recording_stop
        if stop_event is None:
            return False
        stop_event.set()
        return True

    def speak(
        self,
        text: str,
        on_started: Callable[[], None],
        on_finished: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> bool:
        speech_text = prepare_speech_text(text)
        if not speech_text:
            return False
        self.stop_speaking()
        with self._lock:
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._speak_worker,
                args=(speech_text, stop_event, on_started, on_finished, on_error),
                name="friday-speech-synthesis",
                daemon=True,
            )
            self._speaking_stop = stop_event
            self._speaking_thread = thread
            thread.start()
            return True

    def _speak_worker(
        self,
        text: str,
        stop_event: threading.Event,
        on_started: Callable[[], None],
        on_finished: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        try:
            self.stop_wake_word()
            with self._lock:
                wake_thread = self._wake_thread
            if wake_thread is not None and wake_thread is not threading.current_thread():
                wake_thread.join()
            self.engine.speak(text, stop_event, on_started)
        except Exception as error:
            _logger.exception("Speech output worker failed.")
            on_error(error)
        finally:
            with self._lock:
                if self._speaking_stop is stop_event:
                    self._speaking_stop = None
                    self._speaking_thread = None
            on_finished()

    def stop_speaking(self) -> bool:
        with self._lock:
            stop_event = self._speaking_stop
        if stop_event is None:
            return False
        stop_event.set()
        return True

    def close(self) -> None:
        with self._lock:
            stops = (
                self._recording_stop,
                self._wake_stop,
                self._speaking_stop,
            )
            workers = (
                self._recording_thread,
                self._wake_thread,
                self._speaking_thread,
            )
        for stop_event in stops:
            if stop_event is not None:
                stop_event.set()

        stop_all = getattr(self.engine, "stop_all", None)
        if callable(stop_all):
            stop_all()
        for worker in workers:
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=2)
                if worker.is_alive():
                    _logger.warning(
                        "Speech worker %s did not stop within the shutdown timeout.",
                        worker.name,
                    )
