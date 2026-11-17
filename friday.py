"""A local, read-only folder assistant powered by Ollama."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from chat_service import (
    DEFAULT_MODEL,
    DEFAULT_VISION_MODEL,
    HISTORY_MESSAGES,
    MAX_CONTEXT_CHARS,
    MAX_DOCUMENT_CHARS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_IMAGE_BYTES,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
    TOKEN_PATTERN,
    ChatService,
    Document,
    ModelConfig,
    OllamaModelNotFoundError,
    OllamaVisionUnsupportedError,
    ask_ollama,
    find_relevant_documents,
    format_ollama_error,
    load_documents,
    load_document_file,
    load_image,
    make_system_prompt,
    select_model_for_message,
)
from memory_store import MemoryStore
from system_control import handle_system_command


def run_chat(
    folder: Path,
    model: str,
    base_url: str,
    vision_model: str = DEFAULT_VISION_MODEL,
) -> int:
    try:
        documents = load_documents(folder)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    print(f"FRIDAY is ready. Loaded {len(documents)} document(s) from {folder.expanduser().resolve()}.")
    print("Ask about those files, or use /image <path> to ask about one picture.")
    print("Type /help for commands or /quit to exit.")
    try:
        memory_store = MemoryStore()
    except (OSError, sqlite3.Error) as error:
        memory_store = None
        print(f"FRIDAY warning: local memory database is unavailable: {error}", file=sys.stderr)
    chat_service = ChatService(
        ModelConfig(model, vision_model, base_url),
        memory_store=memory_store,
    )
    conversation_id: str | None = None
    if memory_store is not None:
        try:
            conversation_id = memory_store.create_conversation()
        except (OSError, sqlite3.Error) as error:
            print(f"FRIDAY warning: conversation history will not be saved: {error}", file=sys.stderr)
    pending_image: str | None = None

    while True:
        try:
            query = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0

        if not query:
            continue
        if query.lower() in {"/quit", "/exit"}:
            print("Goodbye.")
            return 0
        if query.lower() == "/help":
            print("Commands: /help, /quit, /image <path>.")
            print("Use /image with a .jpg, .jpeg, .png, or .webp file (maximum 10 MB).")
            print("After selecting an image, ask a question; it is sent with that next question only.")
            continue

        if query.lower().startswith("/image"):
            image_argument = query[len("/image"):].strip()
            if not image_argument:
                print("Usage: /image <path to image>")
                continue
            if len(image_argument) >= 2 and image_argument[0] == image_argument[-1] and image_argument[0] in {"'", '"'}:
                image_argument = image_argument[1:-1]
            try:
                pending_image = load_image(Path(image_argument))
            except ValueError as error:
                print(f"FRIDAY error: {error}", file=sys.stderr)
                continue
            print("Image selected. Ask a question about it; it will be sent with your next message only.")
            continue

        has_image = pending_image is not None
        if not has_image:
            handled, action_response = handle_system_command(query)
            if handled:
                if memory_store is not None and conversation_id is not None:
                    try:
                        memory_store.save_message(conversation_id, "user", query)
                        memory_store.save_message(conversation_id, "assistant", action_response)
                    except (OSError, sqlite3.Error):
                        pass
                print(f"\nFRIDAY: {action_response}")
                continue

        if memory_store is not None and conversation_id is not None:
            try:
                memory_store.save_message(conversation_id, "user", query)
            except (OSError, sqlite3.Error) as error:
                print(f"FRIDAY warning: could not save conversation: {error}", file=sys.stderr)
        try:
            answer = chat_service.respond(query, documents, pending_image)
        except RuntimeError as error:
            print(
                f"FRIDAY error: {format_ollama_error(error, has_image)}",
                file=sys.stderr,
            )
            continue

        pending_image = None
        if memory_store is not None and conversation_id is not None:
            try:
                memory_store.save_message(conversation_id, "assistant", answer)
            except (OSError, sqlite3.Error) as error:
                print(f"FRIDAY warning: could not save conversation: {error}", file=sys.stderr)
        if chat_service.memory_error:
            print(
                f"FRIDAY warning: saved memories were not available: {chat_service.memory_error}",
                file=sys.stderr,
            )
        print(f"\nFRIDAY: {answer}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ask a local Ollama model questions about a folder of documents."
    )
    parser.add_argument(
        "folder",
        type=Path,
        nargs="?",
        help="folder to read (.pdf, .txt, .md, .markdown, .rst)",
    )
    gui_commands = parser.add_mutually_exclusive_group()
    gui_commands.add_argument("--gui", action="store_true", help="open the desktop chat window")
    gui_commands.add_argument(
        "--shutdown",
        action="store_true",
        help="stop a running background assistant",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama text model name")
    parser.add_argument(
        "--vision-model",
        default=DEFAULT_VISION_MODEL,
        help="Ollama model used automatically for image questions",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://127.0.0.1:11434",
        help="Ollama API base URL (default: local Ollama)",
    )
    arguments = parser.parse_args()
    if arguments.shutdown:
        from assistant_control import AssistantControlError, send_control

        try:
            send_control("SHUTDOWN")
        except AssistantControlError as error:
            print(f"Could not stop FRIDAY: {error}", file=sys.stderr)
            return 1
        print("FRIDAY assistant shutdown requested.")
        return 0
    if arguments.gui or arguments.folder is None:
        from friday_gui import run_gui

        return run_gui(
            arguments.folder,
            arguments.model,
            arguments.ollama_url,
            arguments.vision_model,
        )
    return run_chat(
        arguments.folder,
        arguments.model,
        arguments.ollama_url,
        arguments.vision_model,
    )


if __name__ == "__main__":
    raise SystemExit(main())
