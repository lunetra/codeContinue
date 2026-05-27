"""CodeContinue Chat - Clean & Professional"""

import json
import threading
import urllib.error
import urllib.request

import sublime
import sublime_plugin

from .api import build_api_headers
from .log import _log


# ==================== SESSIONS ====================
_chat_sessions = {}
_chat_view_ids = set()
_original_layouts = {}
_chat_requesting = set()


DEFAULT_SYSTEM_PROMPT = (
    "You are an expert programmer. Be concise, accurate and practical. "
    "Do not add unnecessary explanations or chit-chat. "
    "When giving code, output only the code block unless asked otherwise. "
    "Keep answers short and to the point."
)


INPUT_SEPARATOR = "\n\n── Type your message • Enter to send • Shift+Enter for new line ──\n"


def _chat_view_append(chat_view, text):
    was_read_only = chat_view.is_read_only()
    chat_view.set_read_only(False)
    chat_view.run_command("append", {"characters": text})
    if was_read_only:
        chat_view.set_read_only(True)
    chat_view.run_command("move_to", {"to": "eof"})


def _get_active_model_settings():
    settings = sublime.load_settings("CodeContinue.sublime-settings")
    models = settings.get("models", [])
    active = next((m for m in models if m.get("enabled", False)), None)
    return active or (models[0] if models else {})


# ==================== BASE CHAT FUNCTIONALITY ====================
class CodeContinueChatCommand(sublime_plugin.TextCommand):
    """Open General Chat"""
    def run(self, edit):
        self._open_chat(general=True)

    def is_enabled(self):
        return True


class CodeContinueChatSelectionCommand(sublime_plugin.TextCommand):
    """Chat About Selection"""
    def run(self, edit):
        self._open_chat(general=False)

    def is_enabled(self):
        return any(not r.empty() for r in self.view.sel())


    def _open_chat(self, general=True):
        window = self.view.window()
        if not window:
            return

        model_config = _get_active_model_settings()
        endpoint = model_config.get("endpoint", "")
        model_name = model_config.get("model", "")
        timeout_ms = model_config.get("timeout_ms", 90000)

        headers = build_api_headers({
            "endpoint": endpoint,
            "api_key": model_config.get("api_keys", [None])[0] if model_config.get("api_keys") else None
        })

        # Split Layout
        wid = window.id()
        _original_layouts[wid] = window.get_layout()
        window.set_layout({
            "cols": [0.0, 0.56, 1.0],
            "rows": [0.0, 1.0],
            "cells": [[0, 0, 1, 1], [1, 0, 2, 1]]
        })

        chat_view = window.new_file()
        chat_view.set_name("CodeContinue Chat")
        chat_view.set_scratch(True)
        chat_view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")

        # Professional UI
        chat_view.settings().set("word_wrap", True)
        chat_view.settings().set("gutter", False)
        chat_view.settings().set("line_numbers", False)
        chat_view.settings().set("draw_indent_guides", False)
        chat_view.settings().set("rulers", [])
        chat_view.settings().set("font_size", 13)

        window.set_view_index(chat_view, 1, 0)
        window.focus_view(chat_view)

        cvid = chat_view.id()
        _chat_view_ids.add(cvid)

        session = {
            "history": [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}],
            "endpoint": endpoint,
            "model": model_name,
            "timeout_s": timeout_ms / 1000.0,
            "headers": headers,
        }

        # Add selected code if not general chat
        if not general:
            selected = "\n".join(self.view.substr(r) for r in self.view.sel() if not r.empty())
            if selected:
                session["history"].append({
                    "role": "user",
                    "content": f"Here is the selected code:\n\n```\n{selected}\n```\nPlease help with this."
                })

        _chat_sessions[cvid] = session

        welcome = (
            "═══ CodeContinue Chat ═══\n\n"
            f"Model: {model_name}\n"
            "Be specific and direct.\n\n" + INPUT_SEPARATOR
        )

        chat_view.run_command("append", {"characters": welcome})
        chat_view.set_read_only(True)


# ==================== EVENT LISTENER ====================
class ChatEventListener(sublime_plugin.EventListener):

    def on_text_command(self, view, command_name, args):
        if view.id() not in _chat_view_ids:
            return None

        if command_name == "insert" and args and args.get("characters") == "\n":
            user_text = self._get_user_input(view)
            if user_text.strip():
                self._send_message(view)
                return ("noop", None)
        return None

    def _get_user_input(self, view):
        content = view.substr(sublime.Region(0, view.size()))
        sep_pos = content.rfind(INPUT_SEPARATOR)
        if sep_pos == -1:
            return ""
        return content[sep_pos + len(INPUT_SEPARATOR):].strip()

    def _send_message(self, view):
        cvid = view.id()
        session = _chat_sessions.get(cvid)
        if not session or cvid in _chat_requesting:
            return

        user_text = self._get_user_input(view)
        if not user_text:
            return

        _chat_requesting.add(cvid)
        self._lock_user_message(view, user_text)
        session["history"].append({"role": "user", "content": user_text})

        _chat_view_append(view, "\n⏳ Thinking...\n")
        self._do_api_call(view, session)

    def _lock_user_message(self, view, user_text):
        content = view.substr(sublime.Region(0, view.size()))
        sep_pos = content.rfind(INPUT_SEPARATOR)
        if sep_pos < 0:
            return
        before = content[:sep_pos]

        view.set_read_only(False)
        view.run_command("select_all")
        view.run_command("left_delete")
        view.run_command("append", {"characters": before + f"\n\n**You:**\n{user_text}\n"})
        view.set_read_only(True)

    def _do_api_call(self, chat_view, session):
        cvid = chat_view.id()
        endpoint = session["endpoint"]
        model = session["model"]
        timeout_s = session["timeout_s"]
        headers = session["headers"]
        history = session["history"]

        data = {
            "model": model,
            "messages": history,
            "max_tokens": 4096,
            "temperature": 0.35,
        }

        def do_request():
            try:
                req = urllib.request.Request(endpoint, data=json.dumps(data).encode(), headers=headers)
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    result = json.loads(resp.read().decode())
                    reply = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

                    if reply:
                        session["history"].append({"role": "assistant", "content": reply})
                        def show():
                            if cvid not in _chat_view_ids: return
                            _chat_remove_thinking(chat_view)
                            _chat_view_append(chat_view, f"\n**Assistant:**\n{reply}\n")
                            _chat_show_input_area(chat_view)
                            _chat_requesting.discard(cvid)
                        sublime.set_timeout(show, 0)
                    else:
                        self._show_error(chat_view, "Empty response from model.")

            except Exception as e:
                self._show_error(chat_view, str(e)[:180])

        thread = threading.Thread(target=do_request)
        thread.daemon = True
        thread.start()

    def _show_error(self, chat_view, msg):
        cvid = chat_view.id()
        def show():
            if cvid not in _chat_view_ids: return
            _chat_remove_thinking(chat_view)
            _chat_view_append(chat_view, f"\n⚠ {msg}\n")
            _chat_show_input_area(chat_view)
            _chat_requesting.discard(cvid)
        sublime.set_timeout(show, 0)

    def _chat_remove_thinking(self, chat_view):
        content = chat_view.substr(sublime.Region(0, chat_view.size()))
        pos = content.rfind("\n⏳ Thinking...\n")
        if pos >= 0:
            cleaned = content[:pos] + content[pos + 17:]
            chat_view.set_read_only(False)
            chat_view.run_command("select_all")
            chat_view.run_command("left_delete")
            chat_view.run_command("append", {"characters": cleaned})
            chat_view.set_read_only(True)

    def _chat_show_input_area(self, chat_view):
        chat_view.set_read_only(False)
        chat_view.run_command("append", {"characters": INPUT_SEPARATOR})
        chat_view.run_command("move_to", {"to": "eof"})

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
            sublime.set_timeout(lambda: window.set_layout(layout), 150)