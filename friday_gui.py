"""Dark Tkinter desktop chat interface for FRIDAY."""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import re
import sqlite3
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from queue import Queue
from tkinter import filedialog, messagebox, scrolledtext
from typing import Any

import friday
from assistant_control import AssistantControlError, AssistantControlServer
from chat_service import ChatService, ModelConfig
from memory_store import Conversation, Memory, MemoryStore
from speech_service import SpeechService


BG = "#0b0d12"
SIDEBAR = "#10131a"
PANEL = "#151922"
PANEL_HOVER = "#1c222d"
TEXT = "#eef1f7"
MUTED = "#8c95a6"
ACCENT = "#6395f6"
ACCENT_HOVER = "#78a5ff"
USER_BUBBLE = "#1b2c47"
ASSISTANT_BUBBLE = "#171c26"
ERROR_BUBBLE = "#351d25"
GREEN = "#62d69b"
RED = "#f07683"
CODE_BLOCK_PATTERN = re.compile(r"```([^\r\n`]*)\r?\n(.*?)```", re.DOTALL)

_logger = logging.getLogger("friday.gui")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _handler = logging.FileHandler(
        Path(tempfile.gettempdir()) / "friday_gui_diagnostics.log",
        encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_handler)


def _enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_awareness = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_awareness is not None:
        set_awareness.argtypes = [ctypes.c_void_p]
        set_awareness.restype = ctypes.c_bool
        if not set_awareness(ctypes.c_void_p(-4)):
            error = ctypes.get_last_error()
            if error not in (5, 87):
                raise ctypes.WinError(error)
        return

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:
        user32.SetProcessDPIAware()
        return

    set_awareness = shcore.SetProcessDpiAwareness
    set_awareness.argtypes = [ctypes.c_int]
    set_awareness.restype = ctypes.c_long
    result = set_awareness(2)
    if result not in (0, -2147024891):
        raise OSError(f"Could not enable Windows DPI awareness (HRESULT {result}).")


class FridayWindow:
    def __init__(
        self,
        root: tk.Tk,
        folder: Path | None,
        model: str,
        base_url: str,
        vision_model: str = friday.DEFAULT_VISION_MODEL,
        lifecycle_events: Queue[tuple[str, object]] | None = None,
        control_server: AssistantControlServer | None = None,
    ) -> None:
        self.root = root
        self._control_server = control_server
        self._shutting_down = False
        self.model = model
        self.vision_model = vision_model
        self.base_url = base_url
        self.memory_store: MemoryStore | None = None
        self.database_error: str | None = None
        try:
            self.memory_store = MemoryStore()
        except (OSError, sqlite3.Error) as error:
            self.database_error = str(error)
            _logger.exception("Could not initialize the local memory database")
        self.chat_service = ChatService(
            ModelConfig(model, vision_model, base_url),
            memory_store=self.memory_store,
        )
        self.speech_service = SpeechService()
        self.voice_enabled = True
        self._recording = False
        self._wake_listening = False
        self._wake_capture_active = False
        self._wake_failed = False
        self._wake_paused_for_tts = False
        self._pending_manual_recording = False
        self._voice_output_pending = False
        self._recognized_speech_count = 0
        self.hands_free: bool = True
        self._bubble_font = tkfont.Font(font=("Segoe UI", 10))
        self._last_chat_width: int | None = None
        self._connection_check_id: str | None = None
        self.folder: Path | None = None
        self.documents: list[friday.Document] = []
        self.pending_image: tuple[str, str] | None = None
        self.attachment_menu: tk.Menu | None = None
        self.current_conversation_id: str | None = None
        self._recent_conversations: list[Conversation] = []
        self._refreshing_recent = False
        self._persistence_warning: str | None = None
        self._busy = False
        self._follow_output = True
        self._sidebar_collapsed = False
        self._events: queue.Queue[tuple[str, Any]] = (
            lifecycle_events if lifecycle_events is not None else queue.Queue()
        )
        self._message_bubbles: list[
            tuple[tk.Canvas, tk.Widget, str, str, list[tk.Label]]
        ] = []
        self._thinking_label: tk.Label | None = None
        self.welcome: tk.Frame | None = None
        self.welcome_window: int | None = None
        self._thinking_phase = 0
        self._last_model = model

        root.title("FRIDAY")
        root.geometry("1180x760")
        root.minsize(760, 540)
        root.configure(bg=BG)
        root.report_callback_exception = self._report_callback_exception
        root.grid_rowconfigure(0, weight=1)
        root.grid_columnconfigure(1, weight=1)

        self._build_sidebar()
        self._build_main_area()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._show_welcome()
        self._initialize_conversations()
        if self.database_error:
            self.status.set(
                f"Local conversation memory is unavailable: {self.database_error}"
            )

        root.after(100, self._drain_events)
        threading.Thread(target=self._connection_worker, daemon=True).start()
        self._activate_wake_word_on_startup()
        if folder is not None:
            self._load_folder(folder)

    def _button(
        self,
        parent: tk.Widget,
        text: str,
        command: Any,
        *,
        accent: bool = False,
        anchor: str = "center",
    ) -> tk.Button:
        background = ACCENT if accent else PANEL
        active = ACCENT_HOVER if accent else PANEL_HOVER
        return tk.Button(
            parent,
            text=text,
            command=command,
            anchor=anchor,
            bg=background,
            fg=TEXT,
            activebackground=active,
            activeforeground=TEXT,
            disabledforeground="#646c7a",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=14,
            pady=10,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )

    def _build_sidebar(self) -> None:
        self.sidebar = tk.Frame(self.root, bg=SIDEBAR, width=248)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(5, weight=1)

        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=20, pady=(22, 24))
        mark = tk.Canvas(brand, width=34, height=34, bg=SIDEBAR, highlightthickness=0)
        mark.pack(side="left", padx=(0, 10))
        mark.create_oval(3, 3, 31, 31, fill="#192b47", outline="")
        mark.create_oval(10, 10, 24, 24, fill=ACCENT, outline="")
        mark.create_oval(15, 15, 19, 19, fill="#eaf1ff", outline="")
        tk.Label(
            brand,
            text="FRIDAY",
            bg=SIDEBAR,
            fg=TEXT,
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left")
        tk.Label(
            brand,
            text="LOCAL AI ASSISTANT",
            bg=SIDEBAR,
            fg=MUTED,
            font=("Segoe UI", 7, "bold"),
        ).pack(side="left", padx=(9, 0), pady=(5, 0))

        self.new_chat_button = self._button(
            self.sidebar, "+   New chat", self._new_chat, accent=True, anchor="w"
        )
        self.new_chat_button.pack(fill="x", padx=16, pady=(0, 28))

        tk.Label(
            self.sidebar,
            text="WORKSPACE",
            bg=SIDEBAR,
            fg="#697386",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 10))
        self.conversation_card = tk.Frame(self.sidebar, bg="#1a202b", padx=12, pady=11)
        self.conversation_card.pack(fill="x", padx=12)
        tk.Label(
            self.conversation_card,
            text="●",
            bg="#1a202b",
            fg=ACCENT,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 9))
        self.conversation_title = tk.Label(
            self.conversation_card,
            text="New conversation",
            bg="#1a202b",
            fg=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.conversation_title.pack(side="left", fill="x", expand=True)

        tk.Label(
            self.sidebar,
            text="RECENT CHATS",
            bg=SIDEBAR,
            fg="#697386",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(16, 7))
        self.recent_listbox = tk.Listbox(
            self.sidebar,
            height=6,
            bg=PANEL,
            fg=TEXT,
            selectbackground="#304870",
            selectforeground=TEXT,
            activestyle="none",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI", 8),
            exportselection=False,
        )
        self.recent_listbox.pack(fill="x", padx=12)
        self.recent_listbox.bind("<<ListboxSelect>>", self._on_recent_selected)

        self.sidebar_spacer = tk.Frame(self.sidebar, bg=SIDEBAR)
        self.sidebar_spacer.pack(fill="both", expand=True)
        self.settings_button = self._button(
            self.sidebar, "⚙   Settings", self._open_settings, anchor="w"
        )
        self.settings_button.pack(fill="x", padx=12, pady=3)
        self.memories_button = self._button(
            self.sidebar, "✦   Memories", self._open_memories, anchor="w"
        )
        self.memories_button.pack(fill="x", padx=12, pady=3)
        self.clear_button = self._button(
            self.sidebar, "⌫   Clear chat", self._clear_chat, anchor="w"
        )
        self.clear_button.pack(fill="x", padx=12, pady=(3, 14))
        tk.Label(
            self.sidebar,
            text="FRIDAY  ·  PRIVATE BY DESIGN",
            bg=SIDEBAR,
            fg="#626b79",
            font=("Segoe UI", 7, "bold"),
        ).pack(fill="x", padx=20, pady=(0, 18))

    def _build_main_area(self) -> None:
        self.main_area = tk.Frame(self.root, bg=BG)
        self.main_area.grid(row=0, column=1, sticky="nsew")
        self.main_area.grid_rowconfigure(1, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.main_area, bg=BG, height=72)
        header.grid(row=0, column=0, sticky="ew", padx=30)
        header.grid_columnconfigure(1, weight=1)
        self.sidebar_toggle = self._button(header, "☰", self._toggle_sidebar)
        self.sidebar_toggle.configure(padx=11, pady=9)
        self.sidebar_toggle.grid(row=0, column=0, sticky="w", pady=16)

        title_box = tk.Frame(header, bg=BG)
        title_box.grid(row=0, column=1, sticky="w", padx=14)
        tk.Label(
            title_box,
            text="FRIDAY",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w")
        self.folder_label = tk.Label(
            title_box,
            text="Choose a folder or attach documents",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.folder_label.pack(anchor="w", pady=(2, 0))

        right_header = tk.Frame(header, bg=BG)
        right_header.grid(row=0, column=2, sticky="e")
        self.model_chip = tk.Label(
            right_header,
            text=f"TEXT  ·  {self.model}",
            bg="#151e2b",
            fg="#adc8ff",
            padx=11,
            pady=7,
            font=("Segoe UI", 8, "bold"),
        )
        self.model_chip.pack(side="left", padx=(0, 12))
        self.connection_dot = tk.Label(
            right_header,
            text="●",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.connection_dot.pack(side="left")
        self.connection_label = tk.Label(
            right_header,
            text="Checking Ollama",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
        )
        self.connection_label.pack(side="left", padx=(5, 0))

        self.chat_area = tk.Frame(self.main_area, bg=BG)
        self.chat_area.grid(row=1, column=0, sticky="nsew", padx=30)
        self.chat_area.grid_rowconfigure(0, weight=1)
        self.chat_area.grid_columnconfigure(0, weight=1)
        self.chat_canvas = tk.Canvas(
            self.chat_area,
            bg=BG,
            highlightthickness=0,
            borderwidth=0,
        )
        self.chat_canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = tk.Scrollbar(
            self.chat_area,
            orient="vertical",
            command=self._scroll_chat,
            bg=SIDEBAR,
            troughcolor=BG,
            activebackground="#343c4a",
            relief="flat",
            borderwidth=0,
            width=12,
        )
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat_canvas.configure(yscrollcommand=self.scrollbar.set)
        self.feed = tk.Frame(self.chat_canvas, bg=BG)
        self.feed_window = self.chat_canvas.create_window(
            (0, 0), window=self.feed, anchor="nw"
        )
        self.feed.bind("<Configure>", self._update_scroll_region)
        self.chat_canvas.bind("<Configure>", self._resize_chat)
        self.chat_canvas.bind("<MouseWheel>", self._on_mousewheel)

        self.composer = tk.Frame(self.main_area, bg=BG)
        self.composer.grid(row=2, column=0, sticky="ew", padx=30, pady=(12, 0))
        self.composer.grid_columnconfigure(0, weight=1)
        self.input_shell = tk.Frame(
            self.composer,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground="#252b37",
            highlightcolor="#39465c",
        )
        self.input_shell.grid(row=0, column=0, sticky="ew")
        self.input_shell.grid_columnconfigure(2, weight=1)
        self.attachment_bar = tk.Frame(self.input_shell, bg=PANEL)
        self.attachment_bar.grid(row=0, column=0, columnspan=6, sticky="ew", padx=14, pady=(10, 0))
        self.message_input = tk.Text(
            self.input_shell,
            height=3,
            wrap="word",
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#304870",
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=10,
            font=("Segoe UI", 10),
            undo=True,
        )
        self.message_input.grid(row=1, column=0, columnspan=6, sticky="ew", padx=0, pady=(0, 4))
        self.message_input.bind("<Return>", self._on_return)
        self.attachment_button = self._button(
            self.input_shell, "＋  Attach", self._show_attachment_menu
        )
        self.attachment_button.configure(padx=12, pady=8)
        self.attachment_button.grid(
            row=2, column=0, sticky="w", padx=10, pady=(0, 10)
        )
        self.microphone_button = self._button(
            self.input_shell, "🎙  Talk", self._toggle_recording
        )
        self.microphone_button.configure(padx=10, pady=8)
        self.microphone_button.grid(
            row=2, column=1, sticky="w", padx=(0, 6), pady=(0, 10)
        )
        tk.Label(
            self.input_shell,
            text="Enter to send  ·  Shift+Enter for a new line",
            bg=PANEL,
            fg="#70798a",
            font=("Segoe UI", 8),
        ).grid(row=2, column=2, sticky="e", padx=10)
        self.voice_toggle_button = self._button(
            self.input_shell, "🔊 Voice on", self._toggle_voice
        )
        self.voice_toggle_button.configure(padx=9, pady=8)
        self.voice_toggle_button.grid(
            row=2, column=3, sticky="e", padx=(0, 4), pady=(0, 10)
        )
        self.stop_speaking_button = self._button(
            self.input_shell, "■ Stop voice", self._stop_speaking
        )
        self.stop_speaking_button.configure(padx=9, pady=8, state="disabled")
        self.stop_speaking_button.grid(
            row=2, column=4, sticky="e", padx=(0, 4), pady=(0, 10)
        )
        self.send_button = self._button(
            self.input_shell, "Send  ➤", self._send, accent=True
        )
        self.send_button.configure(padx=16, pady=8)
        self.send_button.grid(row=2, column=5, sticky="e", padx=(4, 10), pady=(0, 10))

        footer = tk.Frame(self.main_area, bg=BG, height=38)
        footer.grid(row=3, column=0, sticky="ew", padx=30, pady=(8, 8))
        footer.grid_columnconfigure(0, weight=1)
        self.status = tk.StringVar(
            value="Attach documents or choose a folder for context, or ask FRIDAY."
        )
        tk.Label(
            footer,
            textvariable=self.status,
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Segoe UI", 8),
        ).grid(row=0, column=0, sticky="w")
        self.document_count = tk.Label(
            footer,
            text="No folder selected",
            bg=BG,
            fg="#70798a",
            anchor="e",
            font=("Segoe UI", 8),
        )
        self.document_count.grid(row=0, column=3, sticky="e")

    def _initialize_conversations(self) -> None:
        if self.memory_store is None:
            return
        try:
            self._recent_conversations = self.memory_store.list_recent_conversations()
            if self._recent_conversations:
                latest = self._recent_conversations[0]
                self.current_conversation_id = latest.id
                self._refresh_recent_conversations()
                self._open_conversation(latest.id)
            else:
                self._start_new_conversation()
        except (OSError, sqlite3.Error) as error:
            self._report_database_error("Could not load recent conversations", error)

    def _refresh_recent_conversations(self) -> None:
        if self.memory_store is None:
            return
        try:
            self._recent_conversations = self.memory_store.list_recent_conversations()
        except (OSError, sqlite3.Error) as error:
            self._report_database_error("Could not refresh recent conversations", error)
            return

        self._refreshing_recent = True
        try:
            self.recent_listbox.delete(0, "end")
            for conversation in self._recent_conversations:
                self.recent_listbox.insert("end", conversation.title)
                if conversation.id == self.current_conversation_id:
                    index = self.recent_listbox.size() - 1
                    self.recent_listbox.selection_set(index)
                    self.recent_listbox.see(index)
        finally:
            self._refreshing_recent = False

    def _on_recent_selected(self, _event: tk.Event | None = None) -> None:
        if self._refreshing_recent or self._busy:
            return
        selection = self.recent_listbox.curselection()
        if not selection or selection[0] >= len(self._recent_conversations):
            return
        self._open_conversation(self._recent_conversations[selection[0]].id)

    def _open_conversation(self, conversation_id: str) -> None:
        if self.memory_store is None or self._busy:
            return
        try:
            messages = self.memory_store.get_messages(conversation_id)
        except (OSError, sqlite3.Error) as error:
            self._report_database_error("Could not open conversation", error)
            return

        self.current_conversation_id = conversation_id
        self.chat_service.load_history(
            [{"role": message.role, "content": message.content} for message in messages]
        )
        for child in self.feed.winfo_children():
            child.destroy()
        self._message_bubbles.clear()
        self._remove_thinking()
        if messages:
            for message in messages:
                self._append_message(message.role, message.content)
        else:
            self._show_welcome()
        conversation = next(
            (
                item
                for item in self._recent_conversations
                if item.id == conversation_id
            ),
            None,
        )
        self.conversation_title.configure(
            text=conversation.title if conversation else "Conversation"
        )
        self._follow_output = True
        self._refresh_recent_conversations()

    def _start_new_conversation(self) -> None:
        self.current_conversation_id = None
        if self.memory_store is not None:
            try:
                self.current_conversation_id = self.memory_store.create_conversation()
                self._persistence_warning = None
            except (OSError, sqlite3.Error) as error:
                self._report_database_error("Could not create conversation", error)
        self.conversation_title.configure(text="New conversation")
        self._refresh_recent_conversations()

    def _persist_message(self, role: str, content: str) -> str | None:
        if self.memory_store is None or self.current_conversation_id is None:
            return self.database_error
        try:
            self.memory_store.save_message(
                self.current_conversation_id, role, content
            )
        except (OSError, sqlite3.Error) as error:
            _logger.exception("Could not save %s conversation message", role)
            self._persistence_warning = str(error)
            return str(error)
        self._refresh_recent_conversations()
        return None

    def _report_database_error(self, action: str, error: BaseException) -> None:
        self.database_error = str(error)
        self._persistence_warning = str(error)
        _logger.exception("%s", action)
        self.status.set(f"{action}: {error}")

    def _show_welcome(self) -> None:
        self._hide_welcome()
        self.welcome = tk.Frame(self.chat_canvas, bg=BG)
        self.welcome_window = self.chat_canvas.create_window(
            self.chat_canvas.winfo_width() / 2,
            self.chat_canvas.winfo_height() / 2,
            window=self.welcome,
            anchor="center",
        )
        tk.Label(
            self.welcome,
            text="✦",
            bg=BG,
            fg=ACCENT,
            font=("Segoe UI", 28),
        ).pack()
        tk.Label(
            self.welcome,
            text="Good to see you.",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI", 25, "bold"),
        ).pack(pady=(7, 5))
        tk.Label(
            self.welcome,
            text="Ask FRIDAY about your documents or an image.",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(pady=(0, 26))
        suggestions = tk.Frame(self.welcome, bg=BG)
        suggestions.pack()
        for label, prompt in (
            ("Summarize my documents", "Summarize the key points in my documents."),
            ("Ask about an image", "What are the main components shown in this image?"),
        ):
            button = self._button(
                suggestions,
                label,
                lambda value=prompt: self._insert_prompt(value),
            )
            button.pack(side="left", padx=5, pady=4)

    def _hide_welcome(self) -> None:
        if self.welcome is not None and self.welcome.winfo_exists():
            self.welcome.destroy()
        if self.welcome_window is not None:
            self.chat_canvas.delete(self.welcome_window)
        self.welcome = None
        self.welcome_window = None

    def _insert_prompt(self, prompt: str) -> None:
        self.message_input.delete("1.0", "end")
        self.message_input.insert("1.0", prompt)
        self.message_input.focus_set()

    def _update_scroll_region(self, _event: tk.Event | None = None) -> None:
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
        if (
            self.welcome is None
            and self._follow_output
            and self.chat_canvas.winfo_exists()
        ):
            self.chat_canvas.after_idle(self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        if self.chat_canvas.winfo_exists() and self._follow_output:
            self.chat_canvas.yview_moveto(1.0)

    def _scroll_chat(self, *args: str) -> None:
        self.chat_canvas.yview(*args)
        self._follow_output = self.chat_canvas.yview()[1] >= 0.999

    def _resize_chat(self, event: tk.Event) -> None:
        if getattr(self, "_last_chat_width", None) == event.width:
            return
        self._last_chat_width = event.width
        self.chat_canvas.itemconfigure(self.feed_window, width=event.width)
        if self.welcome_window is not None:
            self.chat_canvas.coords(
                self.welcome_window,
                event.width / 2,
                event.height / 2,
            )
        for bubble, content, text, role, wrap_labels in self._message_bubbles:
            self._layout_bubble(
                bubble, content, text, role, event.width, wrap_labels
            )

    def _on_mousewheel(self, event: tk.Event) -> str:
        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            units = int(-1 * (event.delta / 120))
        if units:
            self._scroll_chat("scroll", str(units), "units")
        return "break"

    def _bind_chat_mousewheel(self, widget: tk.Widget) -> None:
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        widget.bind("<Button-4>", self._on_mousewheel, add="+")
        widget.bind("<Button-5>", self._on_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_chat_mousewheel(child)

    def _copy_code(self, button: tk.Button, code: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.root.update_idletasks()
        except tk.TclError as error:
            _logger.exception("could not copy code block to clipboard")
            self.status.set(f"Could not copy code: {error}")
            return

        button.configure(text="Copied!")
        button.after(
            1200,
            lambda: button.configure(text="Copy") if button.winfo_exists() else None,
        )

    def _build_code_response(
        self,
        parent: tk.Widget,
        segments: list[tuple[str, str, str]],
        fill: str,
    ) -> tuple[tk.Frame, list[tk.Label]]:
        content = tk.Frame(parent, bg=fill)
        inner = tk.Frame(content, bg=fill)
        inner.pack(fill="both", expand=True, padx=12, pady=11)
        wrap_labels: list[tk.Label] = []

        for kind, segment, language in segments:
            if kind == "text":
                if not segment:
                    continue
                label = tk.Label(
                    inner,
                    text=segment,
                    bg=fill,
                    fg=TEXT,
                    justify="left",
                    anchor="nw",
                    font=("Segoe UI", 10),
                    wraplength=500,
                )
                label.pack(fill="x", pady=(0, 8))
                wrap_labels.append(label)
                continue

            code_panel = tk.Frame(
                inner,
                bg="#101722",
                highlightthickness=1,
                highlightbackground="#28364a",
            )
            code_panel.pack(fill="x", pady=(3, 10))
            code_header = tk.Frame(code_panel, bg="#101722")
            code_header.pack(fill="x", padx=10, pady=(8, 4))
            tk.Label(
                code_header,
                text=language or "Code",
                bg="#101722",
                fg="#9dbcf5",
                anchor="w",
                font=("Segoe UI", 8, "bold"),
            ).pack(side="left", fill="x", expand=True)
            copy_button = tk.Button(
                code_header,
                text="Copy",
                bg="#202a39",
                fg=TEXT,
                activebackground="#2a3850",
                activeforeground=TEXT,
                disabledforeground=MUTED,
                relief="flat",
                borderwidth=0,
                padx=10,
                pady=4,
                font=("Segoe UI", 8, "bold"),
                cursor="hand2",
            )
            copy_button.configure(
                command=lambda code=segment, button=copy_button: self._copy_code(
                    button, code
                )
            )
            copy_button.pack(side="right")
            code_label = tk.Label(
                code_panel,
                text=segment,
                bg="#101722",
                fg="#dce7fa",
                justify="left",
                anchor="nw",
                padx=10,
                pady=7,
                font=("Consolas", 9),
                wraplength=500,
            )
            code_label.pack(fill="x", padx=2, pady=(0, 3)            )
            wrap_labels.append(code_label)

        return content, wrap_labels

    def _rounded_rectangle(
        self,
        canvas: tk.Canvas,
        width: int,
        height: int,
        fill: str,
    ) -> None:
        radius = min(16, height // 2, width // 2)
        points = [
            radius, 1, width - radius, 1, width - 1, 1,
            width - 1, radius, width - 1, height - radius,
            width - 1, height - 1, width - radius, height - 1,
            radius, height - 1, 1, height - 1, 1, height - radius,
            1, radius, 1, 1, radius, 1,
        ]
        canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=12,
            fill=fill,
            outline=fill,
        )

    def _layout_bubble(
        self,
        bubble: tk.Canvas,
        content: tk.Widget,
        text: str,
        role: str,
        available_width: int,
        wrap_labels: list[tk.Label],
    ) -> None:
        max_width = max(220, min(680, int(available_width * 0.72)))
        font = getattr(self, "_bubble_font", None) or tkfont.Font(font=("Segoe UI", 10))
        natural_width = max(
            (font.measure(line) for line in text.splitlines()),
            default=120,
        ) + 40
        has_code_blocks = not isinstance(content, tk.Label)
        bubble_width = (
            max_width
            if has_code_blocks
            else min(max_width, max(180, natural_width))
        )
        for label in wrap_labels:
            label.configure(wraplength=max(120, bubble_width - 48))
        content.update_idletasks()
        bubble_height = content.winfo_reqheight() + 22
        bubble.configure(width=bubble_width, height=bubble_height)
        bubble.delete("all")
        fill = ERROR_BUBBLE if role == "error" else (
            USER_BUBBLE if role == "user" else ASSISTANT_BUBBLE
        )
        self._rounded_rectangle(bubble, bubble_width, bubble_height, fill)
        bubble.create_window(
            bubble_width / 2,
            bubble_height / 2,
            window=content,
            width=bubble_width - 30,
        )

    def _append_message(self, role: str, text: str) -> None:
        try:
            self._hide_welcome()
            row = tk.Frame(self.feed, bg=BG)
            row.pack(fill="x", padx=24, pady=7)
            side = "right" if role == "user" else "left"
            speaker = "YOU" if role == "user" else "FRIDAY"
            if role == "error":
                speaker = "FRIDAY  ·  ERROR"
            tk.Label(
                row,
                text=speaker,
                bg=BG,
                fg=RED if role == "error" else ("#9dbcf5" if role == "user" else MUTED),
                font=("Segoe UI", 7, "bold"),
            ).pack(anchor="e" if role == "user" else "w", padx=4, pady=(0, 4))
            bubble = tk.Canvas(row, bg=BG, highlightthickness=0, borderwidth=0)
            bubble.pack(side=side)
            fill = ERROR_BUBBLE if role == "error" else (
                USER_BUBBLE if role == "user" else ASSISTANT_BUBBLE
            )
            matches = list(CODE_BLOCK_PATTERN.finditer(text)) if role == "assistant" else []
            if matches:
                segments: list[tuple[str, str, str]] = []
                cursor = 0
                for match in matches:
                    if match.start() > cursor:
                        segments.append(("text", text[cursor:match.start()], ""))
                    segments.append(("code", match.group(2), match.group(1).strip()))
                    cursor = match.end()
                if cursor < len(text):
                    segments.append(("text", text[cursor:], ""))
                content, wrap_labels = self._build_code_response(
                    bubble, segments, fill
                )
            else:
                label = tk.Label(
                    bubble,
                    text=text,
                    bg=fill,
                    fg="#ffb5bd" if role == "error" else TEXT,
                    justify="left",
                    anchor="w",
                    padx=8,
                    pady=8,
                    font=("Segoe UI", 10),
                )
                content = label
                wrap_labels = [label]
            self._message_bubbles.append(
                (bubble, content, text, role, wrap_labels)
            )
            self._layout_bubble(
                bubble,
                content,
                text,
                role,
                max(self.chat_canvas.winfo_width(), 760),
                wrap_labels,
            )
            self._bind_chat_mousewheel(row)
            if role == "user":
                self.conversation_title.configure(
                    text=text[:25] + ("…" if len(text) > 25 else "")
                )
            if role == "user":
                self._follow_output = True
            if self._follow_output:
                self.chat_canvas.after_idle(self._scroll_to_bottom)
            _logger.debug(
                "rendered role=%s feed_children=%d bubble_count=%d "
                "feed_geometry=%s canvas_geometry=%s scrollregion=%s",
                role,
                len(self.feed.winfo_children()),
                len(self._message_bubbles),
                self.feed.winfo_geometry(),
                self.chat_canvas.winfo_geometry(),
                self.chat_canvas.cget("scrollregion"),
            )
        except Exception:
            _logger.exception("failed rendering role=%s text=%r", role, text)
            raise

    def _show_thinking(self, model: str, has_image: bool) -> None:
        self._remove_thinking()
        row = tk.Frame(self.feed, bg=BG)
        row.pack(fill="x", padx=24, pady=7)
        label = tk.Label(
            row,
            text="FRIDAY  ·  THINKING",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 7, "bold"),
        )
        label.pack(anchor="w", padx=4, pady=(0, 4))
        detail = "Analyzing image" if has_image else "Thinking"
        bubble = tk.Label(
            row,
            text=f"{detail}  ·  {model}",
            bg=ASSISTANT_BUBBLE,
            fg="#bdc7d8",
            padx=16,
            pady=12,
            font=("Segoe UI", 9),
        )
        bubble.pack(anchor="w")
        self._thinking_label = bubble
        self._thinking_phase = 0
        self._animate_thinking()
        if self._follow_output:
            self.chat_canvas.after_idle(self._scroll_to_bottom)

    def _animate_thinking(self) -> None:
        if self._thinking_label is None or not self._thinking_label.winfo_exists():
            return
        self._thinking_phase = (self._thinking_phase + 1) % 4
        dots = "." * self._thinking_phase
        prefix = "Analyzing image" if self._last_has_image else "Thinking"
        self._thinking_label.configure(
            text=f"{prefix}{dots}  ·  {self._last_model}"
        )
        self.root.after(450, self._animate_thinking)

    def _remove_thinking(self) -> None:
        if self._thinking_label is not None:
            row = self._thinking_label.master
            row.destroy()
            self._thinking_label = None

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.new_chat_button,
            self.settings_button,
            self.memories_button,
            self.clear_button,
            self.attachment_button,
            self.microphone_button,
            self.send_button,
        ):
            button.configure(state=state)
        self.message_input.configure(state=state)

    def _toggle_sidebar(self) -> None:
        if self._sidebar_collapsed:
            self.sidebar.grid()
        else:
            self.sidebar.grid_remove()
        self._sidebar_collapsed = not self._sidebar_collapsed

    def _new_chat(self) -> None:
        self._clear_chat()
        self.status.set("Started a new conversation.")

    def _clear_chat(self) -> None:
        if self._busy:
            return
        self.chat_service.reset_history()
        self.pending_image = None
        self._refresh_attachment()
        for child in self.feed.winfo_children():
            child.destroy()
        self._message_bubbles.clear()
        self._show_welcome()
        self._start_new_conversation()
        self.status.set("Conversation cleared.")

    def _choose_folder(self) -> None:
        initial_directory = self.folder or (Path.home() / "Documents")
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Choose FRIDAY's documents folder",
            initialdir=str(initial_directory),
        )
        if selected:
            self._load_folder(Path(selected))

    def _load_folder(self, folder: Path) -> None:
        self._set_busy(True)
        self.status.set(f"Loading documents from {folder}...")
        threading.Thread(
            target=self._load_folder_worker,
            args=(folder,),
            daemon=True,
        ).start()

    def _load_folder_worker(self, folder: Path) -> None:
        try:
            resolved_folder = folder.expanduser().resolve()
            documents = friday.load_documents(resolved_folder)
        except (OSError, ValueError) as error:
            self._events.put(("folder_error", str(error)))
            return
        self._events.put(("folder_loaded", (resolved_folder, documents)))

    def _choose_document_files(self) -> None:
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title="Attach documents for FRIDAY",
            filetypes=[
                ("Supported documents", "*.pdf *.txt *.md *.markdown *.rst"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            self._load_document_files([Path(path) for path in selected])

    def _load_document_files(self, paths: list[Path]) -> None:
        self._set_busy(True)
        self.status.set(f"Loading {len(paths)} selected document(s)...")
        threading.Thread(
            target=self._load_document_files_worker,
            args=(paths,),
            daemon=True,
        ).start()

    def _load_document_files_worker(self, paths: list[Path]) -> None:
        documents: list[friday.Document] = []
        errors: list[str] = []
        for path in paths:
            try:
                documents.append(friday.load_document_file(path))
            except (OSError, RuntimeError, ValueError) as error:
                errors.append(str(error))
        self._events.put(("documents_loaded", (documents, errors)))

    def _show_attachment_menu(self) -> None:
        if self.attachment_menu is None:
            self.attachment_menu = tk.Menu(
                self.root,
                tearoff=False,
                bg=PANEL,
                fg=TEXT,
                activebackground="#2a3850",
                activeforeground=TEXT,
                disabledforeground=MUTED,
                relief="flat",
                borderwidth=1,
                activeborderwidth=0,
                font=("Segoe UI", 9),
            )
            self.attachment_menu.add_command(
                label="Image", command=self._choose_image
            )
            self.attachment_menu.add_command(
                label="Documents", command=self._choose_document_files
            )
            self.attachment_menu.add_command(
                label="Folder", command=self._choose_folder
            )
        self.attachment_menu.tk_popup(
            self.attachment_button.winfo_rootx(),
            self.attachment_button.winfo_rooty()
            + self.attachment_button.winfo_height(),
        )

    def _choose_image(self) -> None:
        image_path = filedialog.askopenfilename(
            parent=self.root,
            title="Choose an image",
            filetypes=[
                ("Supported images", "*.jpg *.jpeg *.png *.webp"),
                ("All files", "*.*"),
            ],
        )
        if not image_path:
            return
        try:
            encoded_image = friday.load_image(Path(image_path))
        except ValueError as error:
            self._append_message("error", str(error))
            self.status.set("Could not attach the image.")
            return
        self.pending_image = (encoded_image, Path(image_path).name)
        self._refresh_attachment()
        self.status.set(f"Image attached for your next message: {Path(image_path).name}")

    def _refresh_attachment(self) -> None:
        for child in self.attachment_bar.winfo_children():
            child.destroy()
        if self.pending_image is None:
            self.attachment_bar.grid_remove()
            return
        self.attachment_bar.grid()
        tk.Label(
            self.attachment_bar,
            text=f"▧  {self.pending_image[1]}",
            bg="#202a39",
            fg="#c7d8f8",
            padx=10,
            pady=5,
            font=("Segoe UI", 8),
        ).pack(side="left")
        self._button(
            self.attachment_bar,
            "Remove",
            self._remove_image,
        ).pack(side="left", padx=6)

    def _remove_image(self) -> None:
        self.pending_image = None
        self._refresh_attachment()
        self.status.set("Image attachment removed.")

    def _on_return(self, event: tk.Event) -> str | None:
        if event.state & 0x0001:
            return None
        self._send()
        return "break"

    def _toggle_recording(self) -> None:
        if self._recording:
            self.status.set("Stopping recording...")
            self.speech_service.stop_listening()
            return
        if self._busy:
            return
        if self._voice_output_pending:
            self._pending_manual_recording = True
            self.speech_service.stop_speaking()
            self.status.set("Stopping voice output before opening the microphone...")
            return
        if self._wake_listening:
            self._pending_manual_recording = True
            self.speech_service.stop_wake_word()
            self.status.set("Stopping wake-word listening before recording...")
            return

        self._start_recording()

    def _start_recording(self, *, auto_stop_after_silence: bool = True) -> None:
        if self._busy or self._recording:
            return
        self._recording = True
        self._speech_error = False
        self._recognized_speech_count = 0
        self.microphone_button.configure(
            text="⏹  Stop",
            state="normal",
        )
        self.status.set(
            "Starting in-memory microphone capture on the Windows default input..."
        )
        started = self.speech_service.start_listening(
            self._queue_speech_text,
            lambda: self._events.put(("speech_started", None)),
            lambda: self._events.put(("speech_finished", None)),
            lambda error: self._events.put(("speech_error", str(error))),
            auto_stop_after_silence=auto_stop_after_silence,
            on_processing=lambda: self._events.put(("speech_processing", None)),
        )
        if not started:
            self._recording = False
            self.microphone_button.configure(text="🎙  Talk", state="normal")
            self.status.set("Voice input is already active.")

    def _activate_wake_word_on_startup(self) -> None:
        self._wake_failed = False
        _logger.info("Starting automatic Hey FRIDAY wake-word listening.")
        self._start_wake_word()

    def _start_wake_word(self) -> None:
        if (
            self._wake_failed
            or self._wake_listening
            or self._recording
            or self._busy
            or self._voice_output_pending
            or self._wake_paused_for_tts
        ):
            return
        self._wake_capture_active = False
        _logger.info("Starting wake-word listener worker.")
        started = self.speech_service.start_wake_word(
            lambda detected: self._events.put(("wake_finished", detected)),
            lambda: self._events.put(("wake_started", None)),
            lambda error: self._events.put(("wake_error", str(error))),
            on_detected=lambda: self._events.put(("wake_detected", None)),
        )
        if started:
            self._wake_listening = True
            self.status.set("Loading the local wake-word detector...")
        else:
            _logger.info(
                "Wake-word start deferred because a speech listener is already active."
            )

    def _queue_speech_text(self, text: str) -> None:
        _logger.info(
            "Queued recognized text for the GUI (characters=%d).",
            len(text),
        )
        self._events.put(("speech_text", text))

    def _toggle_voice(self) -> None:
        self.voice_enabled = not self.voice_enabled
        self.voice_toggle_button.configure(
            text="🔊 Voice on" if self.voice_enabled else "🔇 Voice off"
        )
        if not self.voice_enabled:
            self.speech_service.stop_speaking()
            self.status.set("Voice output is off.")
        else:
            self.status.set("Voice output is on.")

    def _stop_speaking(self) -> None:
        if self.speech_service.stop_speaking():
            self.status.set("Stopping FRIDAY's speech...")

    def _speak_response(self, text: str) -> None:
        self._wake_paused_for_tts = True
        if self._wake_listening:
            self.speech_service.stop_wake_word()
        self._voice_output_pending = True
        started = self.speech_service.speak(
            text,
            lambda: self._events.put(("speech_output_started", None)),
            lambda: self._events.put(("speech_output_finished", None)),
            lambda error: self._events.put(("speech_output_error", str(error))),
        )
        if not started:
            self._voice_output_pending = False
            self._wake_paused_for_tts = False
            self._start_wake_word()

    def _close(self) -> None:
        _logger.info("FRIDAY window hidden; background assistant remains active")
        self.root.withdraw()

    def _shutdown_assistant(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        _logger.info("Shutting down the FRIDAY assistant")
        if self._control_server is not None:
            self._control_server.close()
        self.speech_service.close()
        self.root.destroy()

    def _send(self) -> None:
        _logger.debug(
            "send invoked busy=%s folder=%s input=%r",
            self._busy,
            self.folder,
            self.message_input.get("1.0", "end-1c"),
        )
        if self._busy:
            return
        if self._recording:
            self.status.set("Wait for voice capture and transcription to finish before sending.")
            return
        query = self.message_input.get("1.0", "end").strip()
        if not query:
            return
        self.message_input.delete("1.0", "end")
        has_image = self.pending_image is not None
        user_message: dict[str, Any] = {"role": "user", "content": query}
        if has_image:
            user_message["images"] = [self.pending_image[0]]
            query_label = f"{query}\n\nImage attached: {self.pending_image[1]}"
        else:
            query_label = query
        self._append_message("user", query_label)
        self._persist_message("user", query_label)

        selected_model = self.chat_service.model_for_message(user_message)
        self._last_model = selected_model
        self._last_has_image = has_image
        self.model_chip.configure(
            text=f"{'VISION' if has_image else 'TEXT'}  ·  {selected_model}"
        )
        self._set_busy(True)
        self._show_thinking(selected_model, has_image)
        self.status.set(
            f"FRIDAY is analyzing the image with {selected_model}..."
            if has_image
            else f"FRIDAY is thinking with {selected_model}..."
        )
        threading.Thread(
            target=self._ask_worker,
            args=(query, self.pending_image[0] if has_image else None),
            daemon=True,
        ).start()

    def _ask_worker(self, query: str, image: str | None) -> None:
        try:
            _logger.debug("AI request started image=%s query=%r", image is not None, query)
            answer = self.chat_service.respond(query, self.documents, image)
        except RuntimeError as error:
            _logger.exception("AI request failed")
            self._events.put(
                (
                    "answer_error",
                    friday.format_ollama_error(error, image is not None),
                )
            )
            return
        _logger.debug("AI response received characters=%d", len(answer))
        self._events.put(("answer", (answer, self.chat_service.memory_error)))

    def _schedule_connection_check(self) -> None:
        if getattr(self, "_connection_check_id", None) is not None:
            try:
                self.root.after_cancel(self._connection_check_id)
            except Exception:
                pass
        threading.Thread(target=self._connection_worker, daemon=True).start()
        self._connection_check_id = self.root.after(30_000, self._schedule_connection_check)

    def _connection_worker(self) -> None:
        try:
            request = urllib.request.Request(
                f"{self.base_url.rstrip('/')}/api/version",
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                response.read()
        except (urllib.error.URLError, OSError, ValueError) as error:
            self._events.put(("connection", (False, str(error))))
            return
        self._events.put(("connection", (True, "")))

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("FRIDAY settings")
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=BG, padx=24, pady=22)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="Model settings",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", pady=(0, 5))
        tk.Label(
            body,
            text="Choose which local Ollama models FRIDAY uses.",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 18))

        fields: list[tuple[str, tk.Entry]] = []
        for title, value in (
            ("Text model", self.model),
            ("Vision model", self.vision_model),
            ("Ollama URL", self.base_url),
        ):
            tk.Label(
                body,
                text=title,
                bg=BG,
                fg="#b9c1cf",
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(8, 5))
            entry = tk.Entry(
                body,
                bg=PANEL,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                width=44,
                font=("Segoe UI", 10),
            )
            entry.insert(0, value)
            entry.pack(fill="x", ipady=8)
            fields.append((title, entry))

        hands_free_var = tk.BooleanVar(value=getattr(self, "hands_free", True))
        hf_check = tk.Checkbutton(
            body,
            text="Hands-free voice mode (auto-send spoken queries)",
            variable=hands_free_var,
            bg=BG,
            fg=TEXT,
            selectcolor=PANEL,
            activebackground=BG,
            activeforeground=TEXT,
            font=("Segoe UI", 9),
        )
        hf_check.pack(anchor="w", pady=(12, 4))

        actions = tk.Frame(body, bg=BG)
        actions.pack(fill="x", pady=(20, 0))

        def save_settings() -> None:
            values = [entry.get().strip() for _, entry in fields]
            if any(not value for value in values):
                messagebox.showerror(
                    "Invalid settings",
                    "Model names and Ollama URL cannot be empty.",
                    parent=dialog,
                )
                return
            self.model, self.vision_model, self.base_url = values
            self.hands_free = hands_free_var.get()
            self.chat_service.config = ModelConfig(
                self.model,
                self.vision_model,
                self.base_url,
            )
            self.model_chip.configure(text=f"TEXT  ·  {self.model}")
            self._schedule_connection_check()
            self.status.set("Model settings updated.")
            dialog.destroy()

        self._button(actions, "Cancel", dialog.destroy).pack(side="right")
        self._button(actions, "Save settings", save_settings, accent=True).pack(
            side="right", padx=(0, 8)
        )
        dialog.update_idletasks()
        dialog.geometry(f"+{self.root.winfo_rootx() + 180}+{self.root.winfo_rooty() + 120}")

    def _open_memories(self) -> None:
        if self.memory_store is None:
            messagebox.showerror(
                "Memory unavailable",
                f"The local memory database is unavailable: {self.database_error}",
                parent=self.root,
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("FRIDAY memories")
        dialog.configure(bg=BG)
        dialog.geometry("560x500")
        dialog.minsize(480, 400)
        dialog.transient(self.root)
        body = tk.Frame(dialog, bg=BG, padx=20, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="Saved memories",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text="Only save information you explicitly want FRIDAY to remember. "
            "Avoid passwords and other sensitive information.",
            bg=BG,
            fg=MUTED,
            justify="left",
            wraplength=500,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(5, 12))

        memory_list = tk.Listbox(
            body,
            height=8,
            bg=PANEL,
            fg=TEXT,
            selectbackground="#304870",
            selectforeground=TEXT,
            activestyle="none",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI", 9),
            exportselection=False,
        )
        memory_list.pack(fill="both", expand=True)
        preview = tk.Text(
            body,
            height=3,
            wrap="word",
            bg=PANEL,
            fg=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 9),
            state="disabled",
        )
        preview.pack(fill="x", pady=(8, 10))
        memories: list[Memory] = []

        def refresh_memories() -> None:
            nonlocal memories
            try:
                memories = self.memory_store.list_memories()
            except (OSError, sqlite3.Error) as error:
                self._report_database_error("Could not load saved memories", error)
                messagebox.showerror("Memory error", str(error), parent=dialog)
                return
            memory_list.delete(0, "end")
            for memory in memories:
                display = memory.content.replace("\n", " ")
                memory_list.insert("end", display[:90] + ("…" if len(display) > 90 else ""))
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.configure(state="disabled")

        def show_selected(_event: tk.Event | None = None) -> None:
            selection = memory_list.curselection()
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            if selection and selection[0] < len(memories):
                preview.insert("1.0", memories[selection[0]].content)
            preview.configure(state="disabled")

        memory_list.bind("<<ListboxSelect>>", show_selected)
        memory_input = tk.Text(
            body,
            height=3,
            wrap="word",
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 9),
        )
        memory_input.pack(fill="x", pady=(0, 8))

        def add_memory() -> None:
            content = memory_input.get("1.0", "end-1c").strip()
            if not content:
                return
            try:
                self.memory_store.add_memory(content)
            except (OSError, sqlite3.Error, ValueError) as error:
                self._report_database_error("Could not save memory", error)
                messagebox.showerror("Memory error", str(error), parent=dialog)
                return
            memory_input.delete("1.0", "end")
            refresh_memories()
            self.status.set("Saved a user-approved memory locally.")

        def delete_memory() -> None:
            selection = memory_list.curselection()
            if not selection or selection[0] >= len(memories):
                return
            memory = memories[selection[0]]
            if not messagebox.askyesno(
                "Delete memory",
                "Delete this saved memory?",
                parent=dialog,
            ):
                return
            try:
                self.memory_store.delete_memory(memory.id)
            except (OSError, sqlite3.Error) as error:
                self._report_database_error("Could not delete memory", error)
                messagebox.showerror("Memory error", str(error), parent=dialog)
                return
            refresh_memories()
            self.status.set("Deleted saved memory.")

        actions = tk.Frame(body, bg=BG)
        actions.pack(fill="x")
        self._button(actions, "Add memory", add_memory, accent=True).pack(side="left")
        self._button(actions, "Delete selected", delete_memory).pack(
            side="left", padx=(8, 0)
        )
        self._button(actions, "Close", dialog.destroy).pack(side="right")
        refresh_memories()

    def _drain_events(self) -> None:
        try:
            while True:
                try:
                    event, payload = self._events.get_nowait()
                except queue.Empty:
                    break

                if event == "control_show":
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
                    _logger.info("Existing FRIDAY window shown by reconnect request.")
                    if self._wake_failed:
                        self._wake_failed = False
                        self._start_wake_word()
                elif event == "control_shutdown":
                    self._shutdown_assistant()
                    return
                elif event == "folder_loaded":
                    folder, documents = payload
                    if self.folder != folder:
                        self.chat_service.reset_history()
                        for child in self.feed.winfo_children():
                            child.destroy()
                        self._message_bubbles.clear()
                        self._show_welcome()
                        self.conversation_title.configure(text="New conversation")
                        self.pending_image = None
                        self._refresh_attachment()
                        self._start_new_conversation()
                    self.folder = folder
                    self.documents = documents
                    self.folder_label.configure(text=str(folder))
                    self.document_count.configure(
                        text=f"{len(documents)} document(s) loaded"
                    )
                    self.status.set(f"Loaded {len(documents)} document(s).")
                    self._set_busy(False)
                    self._start_wake_word()
                elif event == "documents_loaded":
                    documents, errors = payload
                    existing_paths = {document.path for document in self.documents}
                    added = [
                        document
                        for document in documents
                        if document.path not in existing_paths
                    ]
                    self.documents.extend(added)
                    self.document_count.configure(
                        text=f"{len(self.documents)} document(s) loaded"
                    )
                    if self.folder is None and added:
                        names = ", ".join(
                            Path(document.path).name for document in added[:2]
                        )
                        suffix = "…" if len(added) > 2 else ""
                        self.folder_label.configure(text=f"Attached: {names}{suffix}")
                    if errors:
                        details = "\n".join(errors[:3])
                        if len(errors) > 3:
                            details += (
                                f"\n…and {len(errors) - 3} more file(s) "
                                "could not be loaded."
                            )
                        self._append_message("error", details)
                        self.status.set(
                            f"Loaded {len(added)} document(s); "
                            f"{len(errors)} file(s) could not be loaded."
                        )
                    else:
                        self.status.set(
                            f"Loaded {len(added)} document(s). Ask FRIDAY about them."
                        )
                    self._set_busy(False)
                    self._start_wake_word()
                elif event == "folder_error":
                    self.status.set(f"Could not load folder: {payload}")
                    self._append_message("error", str(payload))
                    self._set_busy(False)
                    self._start_wake_word()
                elif event == "answer":
                    answer, memory_error = payload
                    self._remove_thinking()
                    self.pending_image = None
                    self._refresh_attachment()
                    self._append_message("assistant", answer)
                    persistence_error = self._persist_message("assistant", answer)
                    warning = (
                        persistence_error
                        or self._persistence_warning
                        or memory_error
                    )
                    self.status.set(
                        f"Ready. Saved memory/history warning: {warning}"
                        if warning
                        else "Ready."
                    )
                    self._set_busy(False)
                    if self.voice_enabled:
                        self._speak_response(answer)
                    else:
                        self._start_wake_word()
                elif event == "answer_error":
                    self._remove_thinking()
                    self._append_message("error", str(payload))
                    self.status.set(str(payload))
                    self._set_busy(False)
                    threading.Thread(target=self._connection_worker, daemon=True).start()
                    self._start_wake_word()
                elif event == "connection":
                    connected, details = payload
                    self.connection_dot.configure(fg=GREEN if connected else RED)
                    self.connection_label.configure(
                        text="Ollama connected" if connected else "Ollama offline",
                        fg=GREEN if connected else RED,
                    )
                    if not connected and not self._busy:
                        self.status.set("Ollama is offline. Start Ollama and try again.")
                elif event == "speech_started":
                    self.status.set("🎤 Listening to your command...")
                    self.microphone_button.configure(text="⏹  Stop", state="normal")
                elif event == "speech_processing":
                    self.status.set("Processing your voice...")
                    self.microphone_button.configure(text="⏳ Processing...", state="disabled")
                elif event == "speech_text":
                    recognized = str(payload).strip()
                    if recognized:
                        _logger.info(
                            "GUI received recognized text (characters=%d).",
                            len(recognized),
                        )
                        self._recognized_speech_count += 1
                        current = self.message_input.get("1.0", "end-1c").rstrip()
                        separator = " " if current else ""
                        self.message_input.insert("insert", f"{separator}{recognized}")
                        self.message_input.see("insert")
                        _logger.info(
                            "Transcription inserted into message input (segments=%d).",
                            self._recognized_speech_count,
                        )
                        self.status.set(
                            "Ready — review your recognized text."
                        )
                elif event == "speech_error":
                    self._recording = False
                    self.microphone_button.configure(text="🎙  Talk", state="normal")
                    self._speech_error = True
                    self.status.set(f"Voice input unavailable: {payload}")
                elif event == "speech_finished":
                    self._recording = False
                    self.microphone_button.configure(text="🎙  Talk", state="normal")
                    auto_sent = False
                    if not self._speech_error:
                        if self._recognized_speech_count:
                            if getattr(self, "hands_free", True):
                                auto_sent = True
                                self.root.after_idle(self._send)
                            else:
                                self.status.set(
                                    "Ready — review your recognized text."
                                )
                        else:
                            self.status.set(
                                "No speech was recognized. Check Windows microphone "
                                "privacy and the default input device, then try again."
                            )
                    if not auto_sent and not self._wake_paused_for_tts and not self._wake_failed:
                        self._start_wake_word()
                elif event == "wake_started":
                    self._wake_listening = True
                    self._wake_capture_active = True
                    _logger.info("Wake-word microphone capture is ready.")
                    self.status.set("🟢 Listening for Hey FRIDAY...")
                elif event == "wake_detected":
                    _logger.info("GUI received the local wake-word detection event.")
                    self.status.set("Wake word detected!")
                    try:
                        import winsound

                        winsound.MessageBeep(winsound.MB_OK)
                    except (ImportError, RuntimeError, OSError):
                        _logger.exception("Could not play the wake-word confirmation beep.")
                elif event == "wake_finished":
                    self._wake_listening = False
                    self._wake_capture_active = False
                    if self._pending_manual_recording:
                        self._pending_manual_recording = False
                        self._start_recording()
                    elif payload:
                        self._start_recording(auto_stop_after_silence=True)
                    elif (
                        not self._wake_failed
                        and not self._wake_paused_for_tts
                    ):
                        self._start_wake_word()
                elif event == "wake_error":
                    self._wake_listening = False
                    self._wake_capture_active = False
                    self._wake_failed = True
                    _logger.error("Wake-word listening failed: %s", payload)
                    self.status.set(f"Wake-word listening unavailable: {payload}")
                elif event == "speech_output_started":
                    self._voice_output_pending = True
                    self.stop_speaking_button.configure(state="normal")
                    self.status.set("FRIDAY is speaking. Click Stop voice to interrupt.")
                elif event == "speech_output_finished":
                    self._voice_output_pending = False
                    self._wake_paused_for_tts = False
                    self.stop_speaking_button.configure(state="disabled")
                    if not self._busy and self.voice_enabled:
                        self.status.set("Ready.")
                    if self._pending_manual_recording:
                        self._pending_manual_recording = False
                        self._start_recording()
                    else:
                        self._start_wake_word()
                elif event == "speech_output_error":
                    self._voice_output_pending = False
                    self._wake_paused_for_tts = False
                    self.stop_speaking_button.configure(state="disabled")
                    self.status.set(f"Voice output unavailable: {payload}")
                    if self._pending_manual_recording:
                        self._pending_manual_recording = False
                        self._start_recording()
                    else:
                        self._start_wake_word()
        except Exception:
            _logger.exception("error while draining GUI events")
            raise
        finally:
            if not getattr(self, "_shutting_down", False):
                self.root.after(100, self._drain_events)

    def _report_callback_exception(self, exception: type[BaseException], value: BaseException, tb: Any) -> None:
        _logger.error(
            "Tk callback exception",
            exc_info=(exception, value, tb),
        )


def run_gui(
    folder: Path | None,
    model: str,
    base_url: str,
    vision_model: str = friday.DEFAULT_VISION_MODEL,
) -> int:
    _enable_windows_dpi_awareness()
    lifecycle_events: queue.Queue[tuple[str, object]] = queue.Queue()
    control_server = AssistantControlServer(lifecycle_events)
    _logger.info("Starting FRIDAY assistant process (pid=%d).", os.getpid())
    try:
        owns_assistant = control_server.start_or_reconnect()
    except AssistantControlError as error:
        _logger.exception("Could not start or reconnect to FRIDAY.")
        print(f"FRIDAY could not start: {error}", file=sys.stderr)
        return 1
    if not owns_assistant:
        print("FRIDAY window reopen requested in the existing assistant.")
        return 0

    window: FridayWindow | None = None
    try:
        root = tk.Tk()
        window = FridayWindow(
            root,
            folder,
            model,
            base_url,
            vision_model,
            lifecycle_events,
            control_server,
        )
        _logger.info("FRIDAY GUI initialized; automatic wake listening is enabled.")
        _run_until_shutdown(root, window)
    finally:
        if window is not None and not window._shutting_down:
            window._shutdown_assistant()
        control_server.close()
        _logger.info("FRIDAY assistant process stopped.")
    return 0


def _run_until_shutdown(root: tk.Tk, window: FridayWindow) -> None:
    while not window._shutting_down:
        try:
            if hasattr(root, "update_idletasks"):
                root.update_idletasks()
            root.update()
        except tk.TclError:
            if window._shutting_down:
                break
            raise
        time.sleep(0.01)


if __name__ == "__main__":
    raise SystemExit(run_gui(None, friday.DEFAULT_MODEL, "http://127.0.0.1:11434"))
