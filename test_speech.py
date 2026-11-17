import queue
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from speech_service import (
    _CAPTURE_SCRIPT,
    _SilenceStopDetector,
    _wake_phrase_from_result,
    FasterWhisperEngine,
    SpeechDeviceError,
    SpeechNoInputError,
    SpeechNoRecognitionError,
    SpeechService,
    prepare_speech_text,
)


class MockSpeechEngine:
    def __init__(self) -> None:
        self.recording_started = threading.Event()
        self.speaking_started = threading.Event()
        self.spoken_text = ""
        self.stop_observed = False

    def listen(self, stop_event, on_text, on_started) -> None:
        self.recording_started.set()
        on_started()
        on_text("mocked audio transcript")
        self.stop_observed = stop_event.wait(2)

    def speak(self, text, stop_event, on_started) -> None:
        self.spoken_text = text
        on_started()
        self.speaking_started.set()
        self.stop_observed = stop_event.wait(2)


class FakeWhisperModel:
    def __init__(self, segments=()) -> None:
        self.segments = segments
        self.audio = None
        self.options = None

    def transcribe(self, audio, **options):
        self.audio = audio
        self.options = options
        return iter(self.segments), SimpleNamespace(language="en")


def segment(text: str, *, no_speech_prob: float = 0.01, avg_logprob: float = -0.2):
    return SimpleNamespace(
        text=text,
        no_speech_prob=no_speech_prob,
        avg_logprob=avg_logprob,
    )


class SpeechServiceTests(unittest.TestCase):
    def test_capture_script_returns_raw_pcm_from_ram_without_system_speech_or_files(self) -> None:
        self.assertIn("StopToPcmBytes()", _CAPTURE_SCRIPT)
        self.assertIn("Console.OpenStandardOutput()", _CAPTURE_SCRIPT)
        self.assertIn("_stdout.Write(bytes, 0, bytes.Length)", _CAPTURE_SCRIPT)
        self.assertIn("MemoryStream", _CAPTURE_SCRIPT)
        self.assertNotIn("System.Speech", _CAPTURE_SCRIPT)
        self.assertNotIn("SpeechRecognitionEngine", _CAPTURE_SCRIPT)
        self.assertNotIn("WriteAllBytes", _CAPTURE_SCRIPT)
        self.assertNotIn("SetInputToWaveStream", _CAPTURE_SCRIPT)
        self.assertNotIn("WriteAllBytes", _CAPTURE_SCRIPT)

    def test_wake_detector_uses_local_vosk_and_exact_phrase_grammar(self) -> None:
        import vosk

        class FakeRecognizer:
            def __init__(self) -> None:
                self.audio: list[bytes] = []

            def AcceptWaveform(self, audio: bytes) -> bool:
                self.audio.append(audio)
                return False

            def PartialResult(self) -> str:
                return '{"partial": "hey friday"}'

        recognizer = FakeRecognizer()
        engine = FasterWhisperEngine()
        stop_event = threading.Event()
        started = threading.Event()
        wake_detected = threading.Event()

        with tempfile.TemporaryDirectory() as temporary:
            engine.model_directory = Path(temporary)
            model_path = engine.model_directory / engine._WAKE_MODEL_NAME
            (model_path / "am").mkdir(parents=True)
            (model_path / "am" / "final.mdl").touch()

            def capture(_stop_event, on_started, *, on_chunk, **_kwargs):
                on_started()
                on_chunk(bytearray(b"\x20\x00" * 32))
                return bytearray(), 40

            with (
                patch.object(vosk, "Model", return_value=object()) as load_model,
                patch.object(vosk, "KaldiRecognizer", return_value=recognizer) as make_recognizer,
                patch.object(vosk, "SetLogLevel"),
                patch.object(engine, "_capture_pcm", side_effect=capture),
            ):
                detected = engine.listen_for_wake_word(
                    stop_event,
                    started.set,
                    wake_detected.set,
                )

        self.assertTrue(detected)
        self.assertTrue(stop_event.is_set())
        self.assertTrue(started.is_set())
        self.assertTrue(wake_detected.is_set())
        self.assertEqual(recognizer.audio, [b"\x20\x00" * 32])
        load_model.assert_called_once()
        self.assertEqual(
            make_recognizer.call_args.args[1:],
            (16000, '["hey friday"]'),
        )

    def test_wake_detector_ignores_nonmatching_phrase(self) -> None:
        import vosk

        class FakeRecognizer:
            def AcceptWaveform(self, _audio: bytes) -> bool:
                return True

            def Result(self) -> str:
                return '{"text": "hey fire day"}'

        engine = FasterWhisperEngine()
        stop_event = threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            engine.model_directory = Path(temporary)
            model_path = engine.model_directory / engine._WAKE_MODEL_NAME
            (model_path / "am").mkdir(parents=True)
            (model_path / "am" / "final.mdl").touch()
            with (
                patch.object(vosk, "Model", return_value=object()),
                patch.object(vosk, "KaldiRecognizer", return_value=FakeRecognizer()),
                patch.object(vosk, "SetLogLevel"),
                patch.object(
                    engine,
                    "_capture_pcm",
                    side_effect=lambda _stop, _started, *, on_chunk, **_kwargs: (
                        on_chunk(bytearray(b"\x20\x00" * 32)),
                        (bytearray(), 40),
                    )[1],
                ),
            ):
                detected = engine.listen_for_wake_word(stop_event, lambda: None)

        self.assertFalse(detected)
        self.assertFalse(stop_event.is_set())

    def test_wake_phrase_matches_finalized_and_partial_vosk_results(self) -> None:
        self.assertTrue(_wake_phrase_from_result('{"text": "Hey Friday!"}'))
        self.assertTrue(_wake_phrase_from_result('{"partial": "hey friday"}'))
        self.assertFalse(_wake_phrase_from_result('{"partial": "hey fire day"}'))

    def test_auto_stop_detector_waits_for_silence_after_signal(self) -> None:
        detector = _SilenceStopDetector(threshold=327, silence_seconds=1.5)
        speech = bytearray(b"\x00\x40" * 8)
        silence = bytearray(b"\x00\x00" * 8)

        self.assertFalse(detector.observe(silence, 0.0))
        self.assertFalse(detector.observe(speech, 1.0))
        self.assertFalse(detector.observe(silence, 2.4))
        self.assertTrue(detector.observe(silence, 2.5))
        self.assertFalse(detector.observe(speech, 3.0))

    def test_microphone_pcm_is_received_over_memory_pipe_and_stop_is_sent(self) -> None:
        class FakeStdin:
            data = b""

            def write(self, data):
                self.data += data

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = FakeStdin()
                self.stdout = BytesIO(b"\x10\x00" * 100)
                self.stderr = BytesIO(
                    b"DEVICE:Test microphone\n"
                    b"READY\n"
                    b"AUDIO_INPUT:DETECTED\n"
                    b"MAX_LEVEL:55\n"
                    b"CAPTURE_BYTES:200\n"
                )
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def terminate(self):
                self.returncode = -1

            def kill(self):
                self.returncode = -9

        process = FakeProcess()
        engine = FasterWhisperEngine()
        started = threading.Event()
        stop_event = threading.Event()
        stop_event.set()
        with patch.object(engine, "_start_capture", return_value=process):
            pcm, peak = engine._capture_pcm(stop_event, started.set)

        self.assertTrue(started.is_set())
        self.assertEqual(pcm, bytearray(b"\x10\x00" * 100))
        self.assertEqual(peak, 55)
        self.assertEqual(process.stdin.data, b"STOP\n")

    def test_transcribes_ram_audio_with_english_beam_search_and_vad(self) -> None:
        model = FakeWhisperModel([segment("hello from Whisper")])
        engine = FasterWhisperEngine()
        engine._model = model
        engine._model_backend = "CPU"
        pcm = bytearray(b"\x10\x00" * 16000)
        recognized: list[str] = []

        with patch.object(engine, "_capture_pcm", return_value=(pcm, 50)):
            engine.listen(threading.Event(), recognized.append, lambda: None)

        self.assertEqual(recognized, ["hello from Whisper"])
        self.assertEqual(model.options["language"], "en")
        self.assertEqual(model.options["beam_size"], 5)
        self.assertTrue(model.options["vad_filter"])
        self.assertEqual(model.options["vad_parameters"]["threshold"], 0.35)
        self.assertEqual(model.options["vad_parameters"]["min_speech_duration_ms"], 100)
        self.assertEqual(model.options["no_speech_threshold"], 0.6)
        self.assertEqual(model.options["log_prob_threshold"], -1.0)
        self.assertFalse(model.options["condition_on_previous_text"])
        self.assertTrue((model.audio == 0).all())
        self.assertFalse(any(pcm))

    def test_silent_audio_is_rejected_without_transcript(self) -> None:
        model = FakeWhisperModel([])
        engine = FasterWhisperEngine()
        engine._model = model
        engine._model_backend = "CPU"
        with self.assertRaises(SpeechNoRecognitionError):
            engine._transcribe(bytearray(b"\x00\x00" * 16000))

    def test_empty_audio_is_reported_as_no_microphone_input(self) -> None:
        engine = FasterWhisperEngine()
        with self.assertRaises(SpeechNoInputError):
            engine._transcribe(bytearray())

    def test_decoder_accepted_low_logprob_speech_is_not_rejected_again(self) -> None:
        model = FakeWhisperModel(
            [segment("quiet speech", no_speech_prob=0.1, avg_logprob=-1.2)]
        )
        engine = FasterWhisperEngine()
        engine._model = model
        engine._model_backend = "CPU"
        transcript = engine._transcribe(bytearray(b"\x20\x00" * 8000))

        self.assertEqual(transcript, "quiet speech")
        self.assertEqual(model.options["vad_filter"], True)
        self.assertEqual(model.options["log_prob_threshold"], -1.0)

    def test_decoder_filtered_noise_returns_no_speech(self) -> None:
        model = FakeWhisperModel([])
        engine = FasterWhisperEngine()
        engine._model = model
        engine._model_backend = "CPU"
        with self.assertRaises(SpeechNoRecognitionError):
            engine._transcribe(bytearray(b"\x20\x00" * 8000))

    def test_cuda_model_initialization_falls_back_to_cpu_int8(self) -> None:
        engine = FasterWhisperEngine()
        cpu_model = FakeWhisperModel()
        with tempfile.TemporaryDirectory() as temporary:
            engine.model_directory = Path(temporary)
            (engine.model_directory / "model.bin").touch()
            with (
                patch("ctranslate2.get_cuda_device_count", return_value=1),
                patch(
                    "faster_whisper.WhisperModel",
                    side_effect=[RuntimeError("cuDNN unavailable"), cpu_model],
                ) as create_model,
            ):
                model, backend = engine._get_model()

        self.assertIs(model, cpu_model)
        self.assertEqual(backend, "CPU")
        self.assertEqual(create_model.call_args_list[0].kwargs["compute_type"], "int8_float16")
        self.assertEqual(create_model.call_args_list[1].kwargs["compute_type"], "int8")

    def test_cpu_int8_is_used_when_no_cuda_device_is_available(self) -> None:
        engine = FasterWhisperEngine()
        cpu_model = FakeWhisperModel()
        with tempfile.TemporaryDirectory() as temporary:
            engine.model_directory = Path(temporary)
            (engine.model_directory / "model.bin").touch()
            with (
                patch("ctranslate2.get_cuda_device_count", return_value=0),
                patch.object(engine, "_create_model", return_value=cpu_model) as create_model,
            ):
                model, backend = engine._get_model()

        self.assertIs(model, cpu_model)
        self.assertEqual(backend, "CPU")
        create_model.assert_called_once_with("cpu", "int8")

    def test_gpu_model_is_loaded_once_and_reused(self) -> None:
        engine = FasterWhisperEngine()
        gpu_model = FakeWhisperModel()
        with tempfile.TemporaryDirectory() as temporary:
            engine.model_directory = Path(temporary)
            (engine.model_directory / "model.bin").touch()
            with (
                patch("ctranslate2.get_cuda_device_count", return_value=1),
                patch.object(engine, "_create_model", return_value=gpu_model) as create_model,
            ):
                first = engine._get_model()
                second = engine._get_model()

        self.assertIs(first[0], gpu_model)
        self.assertEqual(first, second)
        create_model.assert_called_once_with("cuda", "int8_float16")

    def test_gpu_inference_failure_retries_with_cpu_int8(self) -> None:
        class FailedGpuModel:
            def transcribe(self, _audio, **_options):
                raise RuntimeError("CUDA runtime unavailable")

        engine = FasterWhisperEngine()
        engine._model = FailedGpuModel()
        engine._model_backend = "CUDA"
        cpu_model = FakeWhisperModel([segment("cpu retry")])
        with patch.object(engine, "_create_model", return_value=cpu_model) as create_model:
            transcript = engine._transcribe(bytearray(b"\x10\x00" * 16000))

        self.assertEqual(transcript, "cpu retry")
        create_model.assert_called_once_with("cpu", "int8")
        self.assertEqual(engine._model_backend, "CPU")

    def test_microphone_errors_are_propagated(self) -> None:
        engine = FasterWhisperEngine()
        with patch.object(
            engine,
            "_capture_pcm",
            side_effect=SpeechDeviceError("No default microphone is available."),
        ):
            with self.assertRaisesRegex(SpeechDeviceError, "microphone"):
                engine.listen(threading.Event(), lambda _text: None, lambda: None)

    def test_transcription_does_not_create_audio_files(self) -> None:
        model = FakeWhisperModel([segment("private spoken words")])
        engine = FasterWhisperEngine()
        engine._model = model
        engine._model_backend = "CPU"
        with tempfile.TemporaryDirectory() as temporary:
            engine.model_directory = Path(temporary)
            pcm = bytearray(b"\x10\x00" * 16000)
            with patch.object(engine, "_capture_pcm", return_value=(pcm, 50)):
                engine.listen(threading.Event(), lambda _text: None, lambda: None)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_stop_ends_push_to_talk_recording(self) -> None:
        engine = MockSpeechEngine()
        service = SpeechService(engine)
        finished = threading.Event()

        self.assertTrue(
            service.start_listening(
                lambda _text: None,
                lambda: None,
                finished.set,
                self.fail,
            )
        )
        self.assertTrue(engine.recording_started.wait(1))
        self.assertTrue(service.stop_listening())
        self.assertTrue(finished.wait(1))
        self.assertTrue(engine.stop_observed)

    def test_wake_word_can_be_stopped_and_excludes_push_to_talk(self) -> None:
        class MockWakeEngine(MockSpeechEngine):
            def listen_for_wake_word(self, stop_event, on_started) -> bool:
                self.recording_started.set()
                on_started()
                self.stop_observed = stop_event.wait(2)
                return False

        engine = MockWakeEngine()
        service = SpeechService(engine)
        finished = threading.Event()
        detected: list[bool] = []

        self.assertTrue(
            service.start_wake_word(
                lambda result: (detected.append(result), finished.set()),
                lambda: None,
                self.fail,
            )
        )
        self.assertTrue(engine.recording_started.wait(1))
        self.assertFalse(
            service.start_listening(
                lambda _text: None,
                lambda: None,
                lambda: None,
                self.fail,
            )
        )
        self.assertTrue(service.stop_wake_word())
        self.assertTrue(finished.wait(1))
        self.assertTrue(engine.stop_observed)
        self.assertEqual(detected, [False])

    def test_wake_word_service_rejects_duplicate_listener(self) -> None:
        class MockWakeEngine(MockSpeechEngine):
            def __init__(self) -> None:
                super().__init__()
                self.listen_count = 0

            def listen_for_wake_word(self, stop_event, on_started) -> bool:
                self.listen_count += 1
                self.recording_started.set()
                on_started()
                stop_event.wait(2)
                return False

        engine = MockWakeEngine()
        service = SpeechService(engine)
        finished = threading.Event()
        callbacks = (
            lambda _detected: finished.set(),
            lambda: None,
            self.fail,
        )

        self.assertTrue(service.start_wake_word(*callbacks))
        self.assertTrue(engine.recording_started.wait(1))
        self.assertFalse(service.start_wake_word(*callbacks))
        self.assertEqual(engine.listen_count, 1)
        self.assertTrue(service.stop_wake_word())
        self.assertTrue(finished.wait(1))

    def test_wake_word_service_reports_microphone_initialization_failure(self) -> None:
        class NoWakeMicrophoneEngine(MockSpeechEngine):
            def listen_for_wake_word(self, _stop_event, _on_started) -> bool:
                raise SpeechDeviceError("No default microphone is available.")

        service = SpeechService(NoWakeMicrophoneEngine())
        finished = threading.Event()
        errors: list[Exception] = []
        detected: list[bool] = []

        self.assertTrue(
            service.start_wake_word(
                lambda result: (detected.append(result), finished.set()),
                lambda: None,
                errors.append,
            )
        )
        self.assertTrue(finished.wait(1))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SpeechDeviceError)
        self.assertEqual(detected, [False])

    def test_speaking_waits_until_wake_word_capture_releases_microphone(self) -> None:
        class MockWakeSpeechEngine(MockSpeechEngine):
            def __init__(self) -> None:
                super().__init__()
                self.wake_stopped = False
                self.speech_started_after_wake = False

            def listen_for_wake_word(self, stop_event, on_started) -> bool:
                self.recording_started.set()
                on_started()
                self.wake_stopped = stop_event.wait(2)
                return False

            def speak(self, text, stop_event, on_started) -> None:
                self.speech_started_after_wake = self.wake_stopped
                self.spoken_text = text
                on_started()

        engine = MockWakeSpeechEngine()
        service = SpeechService(engine)
        wake_finished = threading.Event()
        speech_finished = threading.Event()

        self.assertTrue(
            service.start_wake_word(
                lambda _detected: wake_finished.set(),
                lambda: None,
                self.fail,
            )
        )
        self.assertTrue(engine.recording_started.wait(1))
        self.assertTrue(
            service.speak(
                "hello",
                lambda: None,
                speech_finished.set,
                self.fail,
            )
        )

        self.assertTrue(wake_finished.wait(1))
        self.assertTrue(speech_finished.wait(1))
        self.assertTrue(engine.wake_stopped)
        self.assertTrue(engine.speech_started_after_wake)

    def test_service_reports_microphone_errors(self) -> None:
        class NoMicrophoneEngine(MockSpeechEngine):
            def listen(self, _stop_event, _on_text, _on_started) -> None:
                raise SpeechDeviceError("No default microphone is available.")

        service = SpeechService(NoMicrophoneEngine())
        finished = threading.Event()
        errors: list[Exception] = []
        self.assertTrue(
            service.start_listening(
                lambda _text: None,
                lambda: None,
                finished.set,
                errors.append,
            )
        )
        self.assertTrue(finished.wait(1))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SpeechDeviceError)

    def test_gui_inserts_transcription_into_editable_message_input(self) -> None:
        from friday_gui import FridayWindow

        class FakeInput:
            def __init__(self) -> None:
                self.text = "draft"

            def get(self, _start, _end) -> str:
                return self.text

            def insert(self, _index, text) -> None:
                self.text += text

            def see(self, _index) -> None:
                pass

        class FakeStatus:
            value = ""

            def set(self, value) -> None:
                self.value = value

        class FakeRoot:
            def after(self, *_args) -> None:
                pass

        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("speech_text", "recognized words"))
        app.message_input = FakeInput()
        app._recognized_speech_count = 0
        app.status = FakeStatus()
        app.root = FakeRoot()

        app._drain_events()

        self.assertEqual(app.message_input.text, "draft recognized words")
        self.assertEqual(app._recognized_speech_count, 1)
        self.assertEqual(app.status.value, "Ready — review your recognized text.")

    def test_gui_wake_feedback_and_beep_run_on_event_drain(self) -> None:
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock

        from friday_gui import FridayWindow

        class FakeStatus:
            value = ""

            def set(self, value) -> None:
                self.value = value

        class FakeRoot:
            def after(self, *_args) -> None:
                pass

        beep = Mock()
        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("wake_detected", None))
        app.root = FakeRoot()
        app.status = FakeStatus()
        app._wake_enabled = True
        winsound = SimpleNamespace(MB_OK=0, MessageBeep=beep)

        with patch.dict(sys.modules, {"winsound": winsound}):
            app._drain_events()
        self.assertEqual(app.status.value, "Wake word detected!")
        self.assertEqual(app.status.value, "Wake word detected!")
        beep.assert_called_once_with(0)

    def test_gui_reports_listening_only_after_microphone_is_ready(self) -> None:
        from friday_gui import FridayWindow

        class FakeStatus:
            value = ""

            def set(self, value) -> None:
                self.value = value

        class FakeRoot:
            def after(self, *_args) -> None:
                pass

        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("wake_started", None))
        app.root = FakeRoot()
        app.status = FakeStatus()
        app._wake_enabled = True
        app._drain_events()
        self.assertEqual(app.status.value, "🟢 Listening for Hey FRIDAY...")

    def test_gui_automatically_enables_wake_word_at_startup(self) -> None:
        from friday_gui import FridayWindow

        class FakeStatus:
            value = ""

            def set(self, value) -> None:
                self.value = value

        class FakeSpeechService:
            calls = 0

            def start_wake_word(self, on_finished, on_started, on_error, **kwargs):
                self.calls += 1
                return True

        app = FridayWindow.__new__(FridayWindow)
        app._wake_enabled = True
        app._wake_listening = False
        app._wake_paused_for_tts = False
        app._wake_failed = False
        app._recording = False
        app._busy = False
        app._voice_output_pending = False
        app.status = FakeStatus()
        app.speech_service = FakeSpeechService()
        app._events = queue.Queue()

        app._activate_wake_word_on_startup()

        self.assertTrue(app._wake_enabled)
        self.assertTrue(app._wake_listening)
        self.assertEqual(app.speech_service.calls, 1)

    def test_gui_has_no_manual_wake_or_recording_stop_controls(self) -> None:
        from friday_gui import FridayWindow
        from pathlib import Path

        from friday_gui import __file__ as gui_path

        gui_source = Path(gui_path).read_text(encoding="utf-8")
        self.assertFalse(hasattr(FridayWindow, "_toggle_wake_word"))
        self.assertNotIn("wake_word_button", gui_source)
        self.assertNotIn("Auto-stop after silence", gui_source)
        self.assertNotIn("Stop recording", gui_source)

    def test_gui_keeps_useful_startup_error_after_listener_finishes(self) -> None:
        from friday_gui import FridayWindow

        class FakeStatus:
            value = ""

            def set(self, value) -> None:
                self.value = value

        class FakeRoot:
            def after(self, *_args) -> None:
                pass

        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("wake_error", "No default microphone is available."))
        app._events.put(("wake_finished", False))
        app.root = FakeRoot()
        app.status = FakeStatus()
        app._wake_enabled = True
        app._wake_listening = True
        app._wake_failed = False
        app._pending_manual_recording = False
        app._wake_paused_for_tts = False
        app._start_wake_word = lambda: self.fail("Failed listener must not restart")

        app._drain_events()

        self.assertEqual(
            app.status.value,
            "Wake-word listening unavailable: No default microphone is available.",
        )
        self.assertTrue(app._wake_enabled)
        self.assertTrue(app._wake_failed)

    def test_gui_close_hides_window_without_stopping_background_service(self) -> None:
        from friday_gui import FridayWindow

        class FakeSpeechService:
            closed = False
            wake_listener_active = True

            def close(self) -> None:
                self.closed = True
                self.wake_listener_active = False

        class FakeRoot:
            destroyed = False
            withdrawn = False

            def destroy(self) -> None:
                self.destroyed = True

            def withdraw(self) -> None:
                self.withdrawn = True

        app = FridayWindow.__new__(FridayWindow)
        app.speech_service = FakeSpeechService()
        app.root = FakeRoot()
        app._wake_listening = True

        app._close()

        self.assertFalse(app.speech_service.closed)
        self.assertTrue(app.speech_service.wake_listener_active)
        self.assertTrue(app._wake_listening)
        self.assertFalse(app.root.destroyed)
        self.assertTrue(app.root.withdrawn)

    def test_gui_shutdown_closes_speech_service_and_window(self) -> None:
        from friday_gui import FridayWindow

        class FakeSpeechService:
            closed = False

            def close(self) -> None:
                self.closed = True

        class FakeRoot:
            destroyed = False

            def destroy(self) -> None:
                self.destroyed = True

        app = FridayWindow.__new__(FridayWindow)
        app.speech_service = FakeSpeechService()
        app.root = FakeRoot()
        app._control_server = None
        app._shutting_down = False

        app._shutdown_assistant()

        self.assertTrue(app.speech_service.closed)
        self.assertTrue(app.root.destroyed)

    def test_gui_reconnect_command_restores_hidden_window(self) -> None:
        from friday_gui import FridayWindow

        class FakeRoot:
            calls: list[str] = []

            def deiconify(self) -> None:
                self.calls.append("deiconify")

            def lift(self) -> None:
                self.calls.append("lift")

            def focus_force(self) -> None:
                self.calls.append("focus")

            def after(self, *_args) -> None:
                pass

        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("control_show", None))
        app._shutting_down = False
        app.root = FakeRoot()
        app._wake_enabled = True
        app._wake_listening = True
        app._wake_capture_active = True
        app._wake_failed = False
        app._wake_paused_for_tts = False
        app._pending_manual_recording = False
        app._recording = False
        app._busy = False

        app._drain_events()

        self.assertEqual(app.root.calls, ["deiconify", "lift", "focus"])
        self.assertTrue(app._wake_enabled)
        self.assertTrue(app._wake_listening)

    def test_hidden_gui_event_loop_keeps_assistant_alive_until_shutdown(self) -> None:
        from friday_gui import _run_until_shutdown

        class FakeWindow:
            _shutting_down = False

        class FakeRoot:
            updates = 0

            def update(self) -> None:
                self.updates += 1
                if self.updates == 1:
                    self.withdraw()
                if self.updates == 4:
                    window._shutting_down = True

            def withdraw(self) -> None:
                pass

        window = FakeWindow()
        root = FakeRoot()

        with patch("friday_gui.time.sleep"):
            _run_until_shutdown(root, window)

        self.assertEqual(root.updates, 4)

    def test_gui_reports_command_recording_and_transcription_status(self) -> None:
        from friday_gui import FridayWindow

        class FakeStatus:
            value = ""
            history: list[str] = []

            def set(self, value) -> None:
                self.value = value
                self.history.append(value)

        class FakeButton:
            def configure(self, **_kwargs) -> None:
                pass

        class FakeRoot:
            def after(self, *_args) -> None:
                pass

        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("speech_started", None))
        app._events.put(("speech_processing", None))
        app.root = FakeRoot()
        app.microphone_button = FakeButton()
        app.status = FakeStatus()
        app._wake_enabled = True
        app._speech_error = False
        app._recognized_speech_count = 0
        app._recording = True
        app._wake_paused_for_tts = False
        app._start_wake_word = lambda: None

        app._drain_events()

        self.assertEqual(app.status.value, "Processing your voice...")
        self.assertEqual(
            app.status.history,
            ["🎤 Listening to your command...", "Processing your voice..."],
        )

    def test_manual_recording_auto_stops_after_silence_without_stop_control(self) -> None:
        from friday_gui import FridayWindow

        class FakeStatus:
            def set(self, _value) -> None:
                pass

        class FakeButton:
            options: dict[str, object] = {}

            def configure(self, **options) -> None:
                self.options.update(options)

        class FakeSpeechService:
            options: dict[str, object] = {}

            def start_listening(self, *_args, **options) -> bool:
                self.options = options
                return True

        app = FridayWindow.__new__(FridayWindow)
        app._busy = False
        app._recording = False
        app._wake_enabled = True
        app._speech_error = False
        app._recognized_speech_count = 0
        app._events = queue.Queue()
        app.microphone_button = FakeButton()
        app.status = FakeStatus()
        app.speech_service = FakeSpeechService()

        app._start_recording()

        self.assertTrue(app._recording)
        self.assertTrue(app.speech_service.options["auto_stop_after_silence"])
        self.assertEqual(app.microphone_button.options["state"], "normal")
        self.assertEqual(app.microphone_button.options["text"], "⏹  Stop")
        app.speech_service.stopped = False
        app.speech_service.stop_listening = lambda: setattr(app.speech_service, "stopped", True)
        app._toggle_recording()
        self.assertTrue(app.speech_service.stopped)

    def test_gui_starts_editable_recording_after_wake_detection(self) -> None:
        from friday_gui import FridayWindow

        class FakeStatus:
            value = ""

            def set(self, value) -> None:
                self.value = value

        class FakeWidget:
            def configure(self, **_kwargs) -> None:
                pass

        class FakeRoot:
            def after(self, *_args) -> None:
                pass

        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("wake_finished", True))
        app.root = FakeRoot()
        app.microphone_button = FakeWidget()
        app.status = FakeStatus()
        app._wake_enabled = True
        app._wake_failed = False
        app._wake_paused_for_tts = False
        app._pending_manual_recording = False
        app._recording = False
        app._busy = False
        app._start_recording = lambda **kwargs: setattr(app, "recording_options", kwargs)

        app._drain_events()

        self.assertEqual(app.recording_options, {"auto_stop_after_silence": True})

    def test_windows_tts_remains_interruptible_and_strips_markdown(self) -> None:
        engine = MockSpeechEngine()
        service = SpeechService(engine)
        started = threading.Event()
        finished = threading.Event()
        errors: list[Exception] = []

        self.assertTrue(
            service.speak(
                "Hello **there**.\n\n```python\nprint('secret code')\n```\n"
                "Read [the guide](https://example.test/guide).",
                started.set,
                finished.set,
                errors.append,
            )
        )
        self.assertTrue(engine.speaking_started.wait(1))
        self.assertTrue(started.wait(1))
        self.assertTrue(service.stop_speaking())
        self.assertTrue(finished.wait(1))
        self.assertEqual(engine.spoken_text, "Hello there. Read the guide.")
        self.assertTrue(engine.stop_observed)
        self.assertEqual(errors, [])

    def test_speech_text_drops_code_fences_and_markdown_symbols(self) -> None:
        text = prepare_speech_text(
            "# A title\nUse **bold** and `variable_name`.\n"
            "```sql\nSELECT * FROM users;\n```\n- Final point."
        )
        self.assertEqual(text, "A title Use bold and. Final point.")

    def test_empty_speech_text_does_not_start_output(self) -> None:
        service = SpeechService(MockSpeechEngine())
        self.assertFalse(
            service.speak(
                "```python\nprint('hidden')\n```",
                lambda: None,
                lambda: None,
                self.fail,
            )
        )

    def test_gui_hands_free_auto_sends_query(self) -> None:
        from friday_gui import FridayWindow

        class FakeButton:
            def configure(self, **_kwargs) -> None:
                pass

        class FakeRoot:
            def __init__(self) -> None:
                self.idle_tasks: list[tuple[Any, tuple[Any, ...]]] = []

            def after(self, *_args) -> None:
                pass

            def after_idle(self, func, *args) -> None:
                self.idle_tasks.append((func, args))

        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("speech_finished", None))
        app.root = FakeRoot()
        app.microphone_button = FakeButton()
        app._speech_error = False
        app._recognized_speech_count = 1
        app.hands_free = True
        app._wake_triggered = True
        app._recording = True
        app._wake_paused_for_tts = False
        app._wake_failed = False
        app.sent = False
        app._send = lambda: setattr(app, "sent", True)

        app._drain_events()

        self.assertFalse(app._recording)
        self.assertEqual(len(app.root.idle_tasks), 1)
        app.root.idle_tasks[0][0]()
        self.assertTrue(app.sent)

    def test_gui_manual_mic_preserves_editable_input_without_auto_send(self) -> None:
        from friday_gui import FridayWindow

        class FakeButton:
            def configure(self, **_kwargs) -> None:
                pass

        class FakeRoot:
            def __init__(self) -> None:
                self.idle_tasks: list[tuple[Any, tuple[Any, ...]]] = []

            def after(self, *_args) -> None:
                pass

            def after_idle(self, func, *args) -> None:
                self.idle_tasks.append((func, args))

        class FakeStatus:
            value = ""

            def set(self, value) -> None:
                self.value = value

        app = FridayWindow.__new__(FridayWindow)
        app._events = queue.Queue()
        app._events.put(("speech_finished", None))
        app.root = FakeRoot()
        app.status = FakeStatus()
        app.microphone_button = FakeButton()
        app._speech_error = False
        app._recognized_speech_count = 1
        app.hands_free = True
        app._wake_triggered = False  # Manual mic input
        app._recording = True
        app._wake_paused_for_tts = False
        app._wake_failed = False
        app._start_wake_word = lambda: None
        app.sent = False
        app._send = lambda: setattr(app, "sent", True)

        app._drain_events()

        self.assertFalse(app._recording)
        self.assertEqual(len(app.root.idle_tasks), 0)
        self.assertFalse(app.sent)
        self.assertEqual(app.status.value, "Ready — review your recognized text.")


if __name__ == "__main__":
    unittest.main()
