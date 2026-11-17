# FRIDAY (first milestone)

FRIDAY is a local, read-only assistant for asking questions about a folder of
text documents and explicitly selected images. This version does not control
the computer, execute commands, or send your files to a cloud AI service. It
sends selected document excerpts, selected images, and your chat messages to
the local Ollama API.

## Requirements

- Python 3.10 or newer
- [Ollama for Windows](https://ollama.com/download)
- A text model installed in Ollama, by default `llama3.2:3b`
- Optionally, a vision model for image questions, by default
  `qwen2.5vl:3b`

In PowerShell, install the default text model:

```powershell
ollama pull llama3.2:3b
```

To enable image questions, install the vision model too:

```powershell
ollama pull qwen2.5vl:3b
```

If PowerShell does not recognize `ollama`, use the installed executable
directly:

```powershell
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" pull qwen2.5vl:3b
```

## Run

From this project folder, start the desktop chat window and automatic offline
“Hey FRIDAY” listening:

```powershell
python .\friday.py
```

Choose a documents folder in the window. You can also start the window with a
folder already selected:

```powershell
python .\friday.py --gui "C:\Users\you\Documents"
```

The desktop window supports text chat, folder selection, attaching individual
`.pdf`, `.txt`, `.md`, `.markdown`, and `.rst` documents for retrieval, and
attaching one image to the next message. PDF text is extracted locally with
`pypdf`; scanned/image-only or encrypted PDFs are not OCR-processed.
Text-only questions use `llama3.2:3b`; messages with an image automatically
use `qwen2.5vl:3b`. Responses run in the
background so the window stays responsive. Tkinter, included with most Windows
Python installations, provides the interface.

The desktop interface uses a dark charcoal theme with a collapsible sidebar,
welcome suggestions, aligned chat bubbles, a typing indicator, and Ollama
connection/model status. Fenced Markdown code blocks are shown in monospaced
panels with a per-block Copy button; long replies use the main conversation
scrollbar. Press Enter to send a message; use Shift+Enter to add a line. The
sidebar provides new-chat, clear-chat, document-folder, model settings, recent
conversations, and saved memories. Model settings are kept for the current app
session.

Wake-word listening starts automatically when FRIDAY starts. After saying “Hey
FRIDAY”, speak your command; hands-free voice mode automatically submits your
spoken query, speaks the answer, and seamlessly resumes wake-word listening.
You can also click the composer microphone button (which toggles to Stop when
you wish to finish early or avoid lag). Voice output can be toggled and
interrupted from the composer. A global hotkey (**Win + Shift + F**) summons
FRIDAY from anywhere, and a Windows system tray icon provides background status,
quick muting, and clean shutdown.

FRIDAY also provides local PC and system actions:
- **Audio & Media**: Mute/unmute, volume up/down, play/pause, next/previous track.
- **Hardware Stats**: Real-time battery percentage/charging state, RAM/memory usage.
- **App Launcher**: Quickly open apps (Notepad, Calculator, Spotify, VS Code, Browser, Task Manager).
- **Time & Date**: Instant local clock and date queries.
Speech recognition uses the offline faster-whisper `small.en` model stored in
`models/speech`; it prefers CUDA with `int8_float16` and falls back to CPU
`int8` when the CUDA runtime is unavailable. Microphone audio is kept in RAM,
sent to the local recognizer through an in-memory pipe, and cleared after
transcription. Wake-word detection uses Vosk and the local
`vosk-model-small-en-us-0.15` model. The status shows “🟢 Listening for Hey
FRIDAY...” only after the Vosk recognizer and microphone are ready. Detection
plays a short Windows confirmation beep. Wake detection starts the existing editable Whisper
recording workflow; status messages distinguish command recording,
transcription, and review. Closing the window hides it while the assistant
continues listening in the background. Starting FRIDAY again shows the existing
window and does not create another microphone listener. To stop the background
assistant and release the microphone, run:

```powershell
python .\friday.py --shutdown
```

The local control endpoint listens only on `127.0.0.1`; it carries GUI
show/shutdown commands and no chat or audio data. Wake-word and transcription
audio are processed in RAM only and are never written to disk or sent to
Ollama. Text-to-speech continues to use a Windows-installed voice.

Wake-word mode additionally requires `python -m pip install vosk==0.3.45` and
the official small English model extracted to
`models/speech/vosk-model-small-en-us-0.15` (about 71 MB unpacked).

To keep using the terminal version, run:

```powershell
python .\friday.py "C:\path\to\your\documents"
```

The assistant reads `.pdf`, `.txt`, `.md`, `.markdown`, and `.rst` files in
the chosen folder and its subfolders. Text files larger than 128 KB and PDFs
larger than 20 MB are skipped; at most 100 documents are read. PDF retrieval
uses extracted text and does not include page images. Ask questions in the
terminal; use `/help` or `/quit`.

To ask about one image, select it explicitly with `/image` and then type your
question. The image is included with that next question only:

```text
/image "C:\Users\you\Pictures\diagram.png"
What does this diagram show?
```

Supported image formats are `.jpg`, `.jpeg`, `.png`, and `.webp`; the limit is
10 MB per image. The default `llama3.2:3b` model is text-only; FRIDAY switches
to the configured vision model when the message contains an image. You can
choose different text and vision models when starting FRIDAY:

```powershell
python .\friday.py --gui "C:\path\to\your\documents" --model llama3.2:3b --vision-model qwen2.5vl:3b
```

If the selected vision model is not installed, FRIDAY displays a message with
the command to install it. To select a different Ollama endpoint:

```powershell
python .\friday.py --gui "C:\path\to\your\documents" --ollama-url http://127.0.0.1:11434
```

Keep the default endpoint at `127.0.0.1` for local inference. If you configure a
remote endpoint, your messages and selected excerpts will be sent there.

## Application structure

- `friday.py` provides command-line startup and argument handling.
- `friday_gui.py` contains the Tkinter interface and delegates chat requests to
  the shared service.
- `chat_service.py` owns model configuration, text-versus-image routing,
  document retrieval, Ollama requests, prompt construction, and the
  eight-message model context.
- `memory_store.py` stores conversations and explicitly saved user memories
  locally in SQLite.
- `speech_service.py` runs offline Vosk wake-word detection, faster-whisper
  transcription, and Windows speech synthesis in background workers.

The default model names are defined in `chat_service.py` and can also be
overridden at startup with `--model` and `--vision-model`. The shared service
uses Python's standard library and Ollama's existing local HTTP API. PDF
extraction uses the already-installed `pypdf` package; no additional package
is needed for text-only documents. The Tkinter UI remains independent of the
chat service so another UI can be added later without moving model logic into
the interface.

## Local conversations and memories

The GUI and CLI save conversations locally in
`D:\Projects\friday\memory\friday.sqlite3`, created automatically on first
launch. The GUI lists recent conversations in the sidebar, restores the most
recent conversation at startup, and lets you select an earlier conversation.
Image data is not stored; only the message text and attachment name are saved.

FRIDAY does not automatically turn chat messages into memories. Use
**Memories** in the sidebar to explicitly add a note, view saved notes, or
delete a selected one. Avoid saving passwords and other sensitive information.
Only a limited number of memories with words matching the current question are
added to a prompt. Conversation and memory data stays on this computer except
when included in a request to the configured Ollama endpoint.

## Tests

```powershell
python -m unittest -v
```

## Current limits

This is an early milestone, not unrestricted PC control. Document and memory
retrieval use basic keyword matching, not semantic search. PDF retrieval uses text extraction
only; scanned/image-only PDFs are not OCR-processed. FRIDAY has no desktop
automation or web search support. Ollama supplies the local language model;
FRIDAY needs app-side features to create files, access the web, or control the
computer. Further steps could add broader document formats and narrowly scoped
PC actions with confirmation before consequential changes.
