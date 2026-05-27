"""Chat-about-selection feature: opens a split-pane Markdown chat view."""

import json
import threading
import urllib.error
import urllib.request

import sublime
import sublime_plugin

from .api import build_api_headers
from .log import _log


# chat_view.id() -> session dict
_chat_sessions = {}
_chat_view_ids = set()
_original_layouts = {}
_chat_requesting = set()


CHAT_SYSTEM_PROMPT = (
    "You are a helpful code assistant. The user has selected a piece of code "
    "and wants to discuss it. Provide clear, concise explanations and suggestions. "
    "When showing code, use markdown formatting. Be direct and practical."
)

INPUT_SEPARATOR = "\n── Type below and press Enter to send (close tab to end) ──\n"


def _chat_view_append(chat_view, text):
    was_read_only = chat_view.is_read_only()
    chat_view.set_read_only(False)
    chat_view.run_command("append", {"characters": text})
    if was_read_only:
        chat_view.set_read_only(True)
    chat_view.run_command("move_to", {"to": "eof"})


def _chat_get_user_input(chat_view):
    content = chat_view.substr(sublime.Region(0, chat_view.size()))
    sep_pos = content.rfind(INPUT_SEPARATOR)
    if sep_pos == -1:
        return ""
    return content[sep_pos + len(INPUT_SEPARATOR):].strip()


def _chat_show_input_area(chat_view):
    chat_view.set_read_only(False)
    chat_view.run_command("append", {"characters": INPUT_SEPARATOR})
    chat_view.run_command("move_to", {"to": "eof"})


def _chat_lock_and_format_input(chat_view, user_text):
    content = chat_view.substr(sublime.Region(0, chat_view.size()))
    sep_pos = content.rfind(INPUT_SEPARATOR)
    if sep_pos < 0:
        return

    before = content[:sep_pos]
    chat_view.set_read_only(False)
    chat_view.run_command("select_all")
    chat_view.run_command("left_delete")
    chat_view.run_command("append", {"characters": before + "\n\n You: " + user_text + "\n"})
    chat_view.set_read_only(True)


def _chat_remove_thinking(chat_view):
    content = chat_view.substr(sublime.Region(0, chat_view.size()))
    marker = "\n⏳ Thinking...\n"
    pos = content.rfind(marker)
    if pos >= 0:
        cleaned = content[:pos] + content[pos + len(marker):]
        chat_view.set_read_only(False)
        chat_view.run_command("select_all")
        chat_view.run_command("left_delete")
        chat_view.run_command("append", {"characters": cleaned})
        chat_view.set_read_only(True)


def _get_active_model_settings():
    settings = sublime.load_settings("CodeContinue.sublime-settings")
    models = settings.get("models", [])

    active_model = next((m for m in models if m.get("enabled", False)), None)
    if not active_model and models:
        active_model = models[0]

    return active_model or {}


def _chat_do_api_call(chat_view, session):
    cvid = chat_view.id()
    endpoint = session["endpoint"]
    model = session["model"]
    timeout_s = session["timeout_s"]
    headers = session["headers"]
    history = session["history"]

    if not endpoint or not model:
        sublime.set_timeout(lambda: _chat_view_append(chat_view, "\n⚠ Error: endpoint or model not configured.\n"), 0)
        _chat_requesting.discard(cvid)
        return

    data = {
        "model": model,
        "messages": history,
        "max_tokens": 2048,
        "temperature": 0.5,
    }

    _log(f"Chat: Sending request to {endpoint} | Model: {model}")

    def do_request():
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(data).encode(),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                result = json.loads(response.read().decode())
                reply = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

                if reply:
                    session["history"].append({"role": "assistant", "content": reply})

                    def show_reply():
                        if cvid not in _chat_view_ids:
                            return
                        _chat_remove_thinking(chat_view)
                        _chat_view_append(chat_view, f"\n Assistant: {reply}\n")
                        _chat_show_input_area(chat_view)
                        _chat_requesting.discard(cvid)

                    sublime.set_timeout(show_reply, 0)
                else:
                    def show_empty():
                        if cvid not in _chat_view_ids:
                            return
                        _chat_remove_thinking(chat_view)
                        _chat_view_append(chat_view, "\n⚠ Empty response from model.\n")
                        _chat_show_input_area(chat_view)
                        _chat_requesting.discard(cvid)
                    sublime.set_timeout(show_empty, 0)

        except urllib.error.URLError as e:
            error_msg = str(e)[:200]
            _log(f"Chat: Network error: {error_msg}")

            def show_net_err(err=error_msg):          # Fixed: default argument capture
                if cvid not in _chat_view_ids:
                    return
                _chat_remove_thinking(chat_view)
                _chat_view_append(chat_view, f"\n⚠ Network error: {err}\n")
                _chat_show_input_area(chat_view)
                _chat_requesting.discard(cvid)

            sublime.set_timeout(show_net_err, 0)

        except Exception as e:
            error_msg = str(e)[:200]
            _log(f"Chat: Error: {error_msg}")

            def show_gen_err(err=error_msg):          # Fixed: default argument capture
                if cvid not in _chat_view_ids:
                    return
                _chat_remove_thinking(chat_view)
                _chat_view_append(chat_view, f"\n⚠ Error: {err}\n")
                _chat_show_input_area(chat_view)
                _chat_requesting.discard(cvid)

            sublime.set_timeout(show_gen_err, 0)

    thread = threading.Thread(target=do_request)
    thread.daemon = True
    thread.start()


def _chat_send_message(chat_view):
    cvid = chat_view.id()
    session = _chat_sessions.get(cvid)
    if not session or cvid in _chat_requesting:
        return

    user_text = _chat_get_user_input(chat_view)
    if not user_text:
        return

    _chat_requesting.add(cvid)
    _chat_lock_and_format_input(chat_view, user_text)
    session["history"].append({"role": "user", "content": user_text})
    _chat_view_append(chat_view, "\n⏳ Thinking...\n")
    _chat_do_api_call(chat_view, session)


class ChatEventListener(sublime_plugin.EventListener):
    def on_text_command(self, view, command_name, args):
        if view.id() not in _chat_view_ids:
            return None

        if command_name == "insert" and args and args.get("characters") == "\n":
            if _chat_get_user_input(view) and view.id() not in _chat_requesting:
                _chat_send_message(view)
                return ("noop", None)
        return None

    def on_close(self, view):
        vid = view.id()
        if vid not in _chat_view_ids:
            return

        _chat_view_ids.discard(vid)
        _chat_sessions.pop(vid, None)
        _chat_requesting.discard(vid)

        window = sublime.active_window()
        if window and window.id() in _original_layouts:
            layout = _original_layouts.pop(window.id())
            sublime.set_timeout(lambda: window.set_layout(layout), 100)


class CodeContinueChatCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        window = view.window()
        if not window:
            return

        selected_text = "\n".join(view.substr(r) for r in view.sel() if not r.empty()).strip()
        if not selected_text:
            sublime.status_message("CodeContinue: No text selected")
            return

        file_name = view.file_name() or "untitled"
        syntax = view.settings().get("syntax", "")
        lang = syntax.split("/")[-1].replace(".sublime-syntax", "").lower() if syntax else "unknown"
        base_name = file_name.split("/")[-1].split("\\")[-1]

        # Get model settings
        model_config = _get_active_model_settings()
        endpoint = model_config.get("endpoint", "")
        model_name = model_config.get("model", "")
        timeout_ms = model_config.get("timeout_ms", 30000)

        headers = build_api_headers({
            "endpoint": endpoint,
            "api_key": model_config.get("api_keys", [None])[0] if model_config.get("api_keys") else None
        })

        # Layout
        wid = window.id()
        _original_layouts[wid] = window.get_layout()
        window.set_layout({"cols": [0.0, 0.6, 1.0], "rows": [0.0, 1.0], "cells": [[0,0,1,1], [1,0,2,1]]})
        window.set_view_index(view, 0, 0)

        chat_view = window.new_file()
        chat_view.set_name("Chat - " + base_name)
        chat_view.set_scratch(True)
        chat_view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
        chat_view.settings().set("word_wrap", True)
        chat_view.settings().set("gutter", False)
        chat_view.settings().set("line_numbers", False)
        window.set_view_index(chat_view, 1, 0)
        window.focus_view(chat_view)

        cvid = chat_view.id()
        _chat_view_ids.add(cvid)

        initial_msg = f"Here is the selected code from `{base_name}` ({lang}):\n\n```\n{selected_text}\n```\n\nI'd like to discuss this code."

        session = {
            "history": [
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": initial_msg},
            ],
            "endpoint": endpoint,
            "model": model_name,
            "timeout_s": timeout_ms / 1000.0,
            "headers": headers,
            "code": selected_text,
            "lang": lang,
        }
        _chat_sessions[cvid] = session

        header = f"═══ CodeContinue Chat ═══\nFile: {base_name}  |  Language: {lang}\n\n─── Selected Code ───\n{selected_text}\n"
        chat_view.run_command("append", {"characters": header})
        chat_view.set_read_only(True)

        _chat_view_append(chat_view, "\n⏳ Thinking...\n")
        _chat_requesting.add(cvid)
        _chat_do_api_call(chat_view, session)

    def is_enabled(self):
        return any(not r.empty() for r in self.view.sel())