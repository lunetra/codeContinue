"""suggest.py — CodeContinue inline autocomplete engine.

Architecture (Cursor/Copilot style):
  - FIM (Fill-in-the-Middle): sends PREFIX + SUFFIX, model fills the gap
    → local Ollama models use /api/generate with FIM tokens (best)
    → cloud chat models use a FIM-style prompt with <FILL> marker
  - Smart trigger: only fires when cursor is in an "incomplete" position
    (mid-word, mid-expression) — never when line ends with ; { }
  - Phantom: ghost text shown inline, Tab accepts one line at a time
  - Popup: full suggestion in a panel, Tab/Enter/ESC to handle
  - Grace period: 2s after any accept, no re-trigger
"""

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import sublime
import sublime_plugin

from .log import _log, _log_error
from .model_pool import NoModelsError, get_pool, load_pool_from_settings
from .settings import show_endpoint_config_panel
from .symbol_index import get_symbol_context, on_file_saved
from .text_utils import strip_common_indent


# ──────────────────────────────────────────────────────────────────────────────
# Language registry
# ──────────────────────────────────────────────────────────────────────────────

LANG = {
    "rs":   {"name": "Rust",       "fence": "rust"},
    "py":   {"name": "Python",     "fence": "python"},
    "js":   {"name": "JavaScript", "fence": "javascript"},
    "ts":   {"name": "TypeScript", "fence": "typescript"},
    "tsx":  {"name": "TypeScript", "fence": "tsx"},
    "jsx":  {"name": "JavaScript", "fence": "jsx"},
    "go":   {"name": "Go",         "fence": "go"},
    "java": {"name": "Java",       "fence": "java"},
    "kt":   {"name": "Kotlin",     "fence": "kotlin"},
    "cpp":  {"name": "C++",        "fence": "cpp"},
    "cc":   {"name": "C++",        "fence": "cpp"},
    "hpp":  {"name": "C++",        "fence": "cpp"},
    "c":    {"name": "C",          "fence": "c"},
    "h":    {"name": "C",          "fence": "c"},
    "rb":   {"name": "Ruby",       "fence": "ruby"},
    "php":  {"name": "PHP",        "fence": "php"},
    "sql":  {"name": "SQL",        "fence": "sql"},
    "toml": {"name": "TOML",       "fence": "toml"},
    "yaml": {"name": "YAML",       "fence": "yaml"},
    "yml":  {"name": "YAML",       "fence": "yaml"},
    "json": {"name": "JSON",       "fence": "json"},
    "html": {"name": "HTML",       "fence": "html"},
    "css":  {"name": "CSS",        "fence": "css"},
    "scss": {"name": "SCSS",       "fence": "scss"},
    "sh":   {"name": "Bash",       "fence": "bash"},
    "bash": {"name": "Bash",       "fence": "bash"},
    "md":   {"name": "Markdown",   "fence": "markdown"},
    "_default": {"name": "code",   "fence": ""},
}

SYNTAX_MAP = {
    "rust": "rs", "python": "py", "javascript": "js", "typescript": "ts",
    "tsx": "tsx", "jsx": "jsx", "go": "go", "java": "java", "kotlin": "kt",
    "c++": "cpp", "c": "c", "ruby": "rb", "php": "php", "sql": "sql",
    "toml": "toml", "yaml": "yaml", "json": "json", "html": "html",
    "css": "css", "scss": "scss", "bash": "sh", "shell": "sh",
    "shellscript": "sh", "markdown": "md",
}


def detect_language(view):
    fp = view.file_name() or ""
    if fp:
        ext = os.path.splitext(fp)[1].lstrip(".").lower()
        if ext in LANG:
            return LANG[ext], ext
    syntax = view.syntax()
    if syntax:
        ext = SYNTAX_MAP.get(syntax.name.lower())
        if ext:
            return LANG[ext], ext
    return LANG["_default"], "_default"


# ──────────────────────────────────────────────────────────────────────────────
# Trigger analysis — "is this a good place to suggest?"
# ──────────────────────────────────────────────────────────────────────────────

# Line endings that mean "statement complete, nothing to fill here"
_COMPLETE_LINE_ENDS = re.compile(r"[;{},\[\]]\s*$")


def should_trigger(view, cursor, min_chars: int) -> bool:
    """
    Return True if the cursor position is a good candidate for autocomplete.

    Rules (Cursor/Copilot style):
    - Current line must have >= min_chars non-whitespace characters
    - Line must NOT end with a statement-terminator (;  {  }  ,  etc.)
      because those positions are already "complete"
    - Cursor must be at end-of-typed-content on the current line
      (not in the middle of existing text — that's editing, not appending)
    """
    row, col = view.rowcol(cursor)
    line_start  = view.text_point(row, 0)
    line_end    = view.text_point(row + 1, 0) - 1 if row + 1 <= view.rowcol(view.size())[0] else view.size()
    line_before = view.substr(sublime.Region(line_start, cursor))
    line_after  = view.substr(sublime.Region(cursor, line_end)).rstrip("\n")

    # Not enough content yet
    if len(line_before.strip()) < min_chars:
        return False

    # Line is already "complete" — no fill needed
    if _COMPLETE_LINE_ENDS.search(line_before):
        return False

    # If there's significant text AFTER cursor on this line, user is editing
    # existing code (not appending). Don't interrupt them.
    if len(line_after.strip()) > 8:
        return False

    return True


# ──────────────────────────────────────────────────────────────────────────────
# Context helpers
# ──────────────────────────────────────────────────────────────────────────────

def list_sibling_modules(file_path, ext):
    if not file_path or not ext or ext == "_default":
        return []
    d   = os.path.dirname(file_path)
    cur = os.path.basename(file_path)
    try:
        return sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(d)
            if f.endswith("." + ext) and f != cur and not f.startswith(".")
        )[:20]
    except OSError:
        return []


def build_extra_context(view, file_path, ext, settings):
    parts = []
    siblings = list_sibling_modules(file_path, ext)
    if siblings:
        parts.append(
            "Sibling .{0} files: {1}".format(ext, ", ".join(siblings))
        )
    if ext == "rs":
        base = os.path.basename(file_path or "")
        if base in ("mod.rs", "lib.rs"):
            parts.append(
                "This is {0} — declare submodules with `mod X;` and "
                "re-export with `pub use X::...;`. "
                "Use ONLY the sibling filenames listed above.".format(base)
            )
        if settings.get("index_neighboring_files", False) and file_path:
            vc = view.substr(sublime.Region(0, view.size()))
            sym = get_symbol_context(file_path, vc, index_neighbors=True, max_chars=300)
            if sym:
                parts.append(sym)
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# FIM prompt builders
# ──────────────────────────────────────────────────────────────────────────────

# qwen2.5-coder, phi, deepseek-coder FIM tokens
_FIM_PRE = "<|fim_prefix|>"
_FIM_SUF = "<|fim_suffix|>"
_FIM_MID = "<|fim_middle|>"


def build_fim_payload(slot, prefix: str, suffix: str, system: str) -> dict:
    """Ollama /api/generate with FIM tokens — the correct way for local models."""
    return {
        "model":  slot.model_id,
        "system": system,
        "prompt": _FIM_PRE + prefix + _FIM_SUF + suffix + _FIM_MID,
        "stream": True,
        "options": {
            "temperature": slot.temperature,
            "top_p":       slot.top_p,
            "num_predict": slot.max_tokens,
        },
    }


def build_chat_payload(slot, prefix: str, suffix: str,
                       lang: dict, extra_context: str) -> dict:
    """OpenAI-compatible chat with FIM-style <FILL> prompt for cloud models."""
    name  = lang["name"]
    fence = lang["fence"]

    system = (
        "You are a {name} code autocomplete engine. "
        "Generate ONLY the code that fills the <FILL> marker. "
        "Rules: no markdown, no backticks, no explanations, no comments. "
        "Match existing indentation. Output 1-6 lines maximum."
    ).format(name=name)

    if extra_context:
        system += "\n\nProject context:\n" + extra_context

    if suffix.strip():
        user = (
            "Fill in <FILL> in this {name} code.\n"
            "Output ONLY the raw code for <FILL>:\n\n"
            "```{fence}\n{pre}<FILL>{suf}\n```"
        ).format(name=name, fence=fence, pre=prefix, suf=suffix[:500])
    else:
        user = (
            "Continue this {name} code from where it ends.\n"
            "Output ONLY the raw continuation:\n\n"
            "```{fence}\n{pre}\n```"
        ).format(name=name, fence=fence, pre=prefix)

    return {
        "model":       slot.model_id,
        "messages":    [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "max_tokens":  slot.max_tokens,
        "temperature": slot.temperature,
        "top_p":       slot.top_p,
        "stream":      True,
    }


def build_system_for_fim(lang: dict, extra_context: str) -> str:
    """System prompt for Ollama FIM mode."""
    name = lang["name"]
    system = (
        "You are a {name} code autocomplete engine. "
        "Complete ONLY what belongs between the prefix and suffix. "
        "Output raw code only — no markdown, no comments, no explanations. "
        "Match existing indentation and style. 1-6 lines maximum."
    ).format(name=name)
    if extra_context:
        system += "\n\nProject context:\n" + extra_context
    return system


# ──────────────────────────────────────────────────────────────────────────────
# Output cleaning
# ──────────────────────────────────────────────────────────────────────────────

_SPECIAL_TOKEN_RE = re.compile(
    r"<\|(?:im_start|im_end|endoftext|begin_of_text|end_of_text|"
    r"eot_id|start_header_id|end_header_id|fim_prefix|fim_middle|"
    r"fim_suffix|file_separator|user|assistant|system)\|>"
    r"|\[(?:END_OF_TEXT|/?INST)\]"
)

_COMMENT_LINE_RE = re.compile(
    r"^\s*(?://.*|#.*|/\*.*\*/|--.*)\s*$"
)


def clean_completion(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"^\s*```[a-zA-Z_+\-]*\s*\n?", "", text)
    if "```" in text:
        text = text.split("```", 1)[0]
    text = _SPECIAL_TOKEN_RE.sub("", text)
    lines = text.split("\n")
    # Remove pure comment/explanation lines
    lines = [l for l in lines if not _COMMENT_LINE_RE.match(l)]
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Response alignment
# ──────────────────────────────────────────────────────────────────────────────

def align_to_cursor(completion: str, view, req_cursor: int) -> str:
    """
    When the response arrives, the user may have typed more.
    Strip the already-typed portion from the completion so it starts
    exactly at the current cursor position.
    """
    sel = view.sel()
    if not sel:
        return completion
    cur = sel[0].begin()
    if cur <= req_cursor:
        return completion

    typed = view.substr(sublime.Region(req_cursor, cur))
    if not typed.strip():
        return completion

    typed = typed.rstrip()
    if completion.startswith(typed):
        rest = completion[len(typed):]
        return rest.lstrip("\n")

    idx = completion.find(typed)
    if idx != -1:
        return completion[idx + len(typed):].lstrip("\n")

    # Incompatible — discard if typed is substantial
    if len(typed) >= 8:
        return ""

    return completion


def strip_line_overlap(completion: str, view, cursor: int) -> str:
    """
    Strip the part of the completion that overlaps with what the user
    already has on the current line before the cursor.
    e.g. user typed 'pub use ', completion starts with 'pub use session::*;'
    → strip 'pub use ', leave 'session::*;'
    """
    row, _ = view.rowcol(cursor)
    line_start = view.text_point(row, 0)
    line_text  = view.substr(sublime.Region(line_start, cursor)).rstrip()
    if not line_text or not completion:
        return completion
    lines = completion.split("\n")
    if lines and lines[0].startswith(line_text):
        lines[0] = lines[0][len(line_text):]
        while lines and lines[0].strip() == "":
            lines.pop(0)
    elif lines and line_text.endswith(lines[0]):
        # completion is entirely covered by what's already typed
        lines.pop(0)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Streaming parsers
# ──────────────────────────────────────────────────────────────────────────────

def _stream_ollama(resp, on_token, on_done, cancel):
    """Parse Ollama /api/generate NDJSON stream."""
    full = []
    while True:
        if cancel.is_set():
            return
        raw = resp.readline()
        if not raw:
            break
        try:
            chunk = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        tok = chunk.get("response", "")
        if tok:
            full.append(tok)
            on_token(tok)
        if chunk.get("done"):
            break
    on_done("".join(full))


def _stream_openai(resp, on_token, on_done, cancel):
    """Parse OpenAI SSE stream."""
    full = []
    while True:
        if cancel.is_set():
            return
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line or not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            chunk   = json.loads(data)
            delta   = chunk["choices"][0].get("delta", {})
            tok     = delta.get("content") or ""
        except (KeyError, IndexError, json.JSONDecodeError):
            continue
        if tok:
            full.append(tok)
            on_token(tok)
    on_done("".join(full))


# ──────────────────────────────────────────────────────────────────────────────
# Animated status spinner
# ──────────────────────────────────────────────────────────────────────────────

_STATUS_KEY = "zzz_cc"
_SPINNER    = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class _Spinner:
    def __init__(self):
        self._lock   = threading.Lock()
        self._timer  = None
        self._view   = None
        self._label  = ""
        self._idx    = 0
        self._active = False

    def start(self, view, label):
        with self._lock:
            self._view   = view
            self._label  = label
            self._idx    = 0
            self._active = True
        self._tick()

    def stop(self):
        with self._lock:
            self._active = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            view = self._view
        if view:
            sublime.set_timeout(lambda: view.erase_status(_STATUS_KEY), 0)

    def set_label(self, label):
        with self._lock:
            self._label = label

    def _tick(self):
        with self._lock:
            if not self._active:
                return
            spin  = _SPINNER[self._idx % len(_SPINNER)]
            label = self._label
            view  = self._view
            self._idx += 1
        if view:
            sublime.set_timeout(
                lambda: view.set_status(_STATUS_KEY, "{0} {1}".format(spin, label)),
                0
            )
        with self._lock:
            if self._active:
                self._timer = threading.Timer(0.1, self._tick)
                self._timer.start()


# ──────────────────────────────────────────────────────────────────────────────
# Phantom display
# ──────────────────────────────────────────────────────────────────────────────

def _lines_to_html(lines):
    escaped = [html.escape(l).replace(" ", "&nbsp;") for l in lines]
    return "<br>".join(escaped)


def _phantom_html(lines):
    return (
        '<body id="cc-p">'
        '<span style="color:#6e7681;font-style:italic;">{0}</span>'
        '</body>'
    ).format(_lines_to_html(lines))


def show_phantom(view, cursor, lines):
    clear_phantoms(view)
    if not lines:
        return
    ps = sublime.PhantomSet(view, "cc_suggest")
    ps.update([sublime.Phantom(
        sublime.Region(cursor, cursor),
        _phantom_html(lines),
        sublime.LAYOUT_INLINE,
    )])
    phantoms[view.id()] = (ps, list(lines), cursor)
    view.settings().set("code_continue_visible", True)


def clear_phantoms(view):
    vid = view.id()
    if vid in phantoms:
        phantoms[vid][0].update([])
        del phantoms[vid]
    view.settings().erase("code_continue_visible")


# ──────────────────────────────────────────────────────────────────────────────
# Popup display
# ──────────────────────────────────────────────────────────────────────────────

_popup_store = {}   # view.id() -> (cursor, full_text)


def show_popup(view, cursor, text):
    _popup_store[view.id()] = (cursor, text)
    view.settings().set("code_continue_popup_visible", True)
    lines     = text.split("\n")
    code_html = _lines_to_html(lines)
    content   = (
        '<body style="margin:0;padding:0;">'
        '<div style="padding:6px 10px;font-family:monospace;font-size:0.95em;'
        'color:#cdd9e5;background:#22272e;border-bottom:1px solid #444;">'
        '{code}</div>'
        '<div style="padding:4px 10px;background:#2d333b;">'
        '<a href="accept" style="color:#57ab5a;text-decoration:none;'
        'font-weight:bold;">✓ Accept (Tab/Enter)</a>'
        '&nbsp;&nbsp;&nbsp;'
        '<a href="dismiss" style="color:#e5534b;text-decoration:none;">'
        '✗ Dismiss (Esc)</a>'
        '</div>'
        '</body>'
    ).format(code=code_html)
    view.show_popup(
        content,
        flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
        location=cursor,
        max_width=750,
        max_height=400,
        on_navigate=lambda href: _popup_nav(view, href),
        on_hide=lambda: _popup_on_hide(view),
    )


def _popup_nav(view, href):
    view.settings().erase("code_continue_popup_visible")
    entry = _popup_store.pop(view.id(), None)
    if href == "accept" and entry:
        _, text = entry
        view.run_command("code_continue_accept_popup", {"text": text})
    view.hide_popup()


def _popup_on_hide(view):
    # Delayed clear so keybindings can still see the setting
    def _clear():
        _popup_store.pop(view.id(), None)
        view.settings().erase("code_continue_popup_visible")
    sublime.set_timeout(_clear, 100)


# ──────────────────────────────────────────────────────────────────────────────
# Plugin state
# ──────────────────────────────────────────────────────────────────────────────

phantoms         = {}
pending_requests = {}
suppress_clear   = set()
accept_grace     = {}    # view.id() -> float (epoch seconds)
_auto_timers     = {}
_spinner         = _Spinner()


def _cancel_timer(vid):
    t = _auto_timers.pop(vid, None)
    if t:
        t.cancel()


def _reload_pool():
    s  = sublime.load_settings("CodeContinue.sublime-settings")
    ok = load_pool_from_settings(s)
    if ok:
        _log("Pool: {0}".format(get_pool().model_names()))
    return ok


# ──────────────────────────────────────────────────────────────────────────────
# Event listener
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueListener(sublime_plugin.EventListener):

    def on_modified(self, view):
        vid = view.id()

        # Clear phantom if user typed something (not during Tab-accept)
        if vid in phantoms:
            if vid not in suppress_clear and time.time() >= accept_grace.get(vid, 0):
                clear_phantoms(view)

        settings = sublime.load_settings("CodeContinue.sublime-settings")
        if not settings.get("auto_trigger", True):
            return

        # Don't trigger during or right after accept
        if vid in suppress_clear or time.time() < accept_grace.get(vid, 0):
            _cancel_timer(vid)
            return

        # Language gate
        trigger_langs = settings.get("trigger_language", [])
        _, ext = detect_language(view)
        syntax = view.syntax()
        syntax_name = syntax.name.lower() if syntax else ""
        if trigger_langs and not (
            ext in [t.lower() for t in trigger_langs]
            or any(t.lower() in syntax_name for t in trigger_langs)
        ):
            return

        # Check if cursor position is worth completing
        sel = view.sel()
        if not sel:
            return
        cursor   = sel[0].begin()
        min_chars = settings.get("auto_trigger_min_chars", 3)
        if not should_trigger(view, cursor, min_chars):
            _cancel_timer(vid)
            return

        # Debounce
        _cancel_timer(vid)
        delay = settings.get("auto_trigger_delay", 0.8)
        timer = threading.Timer(
            delay,
            lambda: sublime.set_timeout(
                lambda: view.run_command("code_continue_suggest"), 0
            )
        )
        _auto_timers[vid] = timer
        timer.start()

    def on_text_command(self, view, command_name, args):
        """Intercept Tab/Enter when popup is open."""
        if view.id() not in _popup_store:
            return None
        chars    = (args or {}).get("characters", "")
        is_enter = command_name == "insert" and chars in ("\n", "\r")
        is_tab   = command_name in (
            "insert_best_completion", "indent",
            "insert_snippet", "reindent"
        ) or (command_name == "insert" and chars == "\t")
        if is_enter or is_tab:
            view.run_command("code_continue_accept_popup")
            return ("noop", None)
        return None

    def on_post_save(self, view):
        fp = view.file_name()
        if fp:
            on_file_saved(fp)
        _cancel_timer(view.id())


# ──────────────────────────────────────────────────────────────────────────────
# Suggest command
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueSuggestCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view     = self.view
        settings = sublime.load_settings("CodeContinue.sublime-settings")

        if not _reload_pool():
            view.set_status(_STATUS_KEY, "CC: no models configured")
            sublime.set_timeout(lambda: show_endpoint_config_panel(view), 100)
            return

        sel = view.sel()
        if len(sel) != 1:
            return

        cursor    = sel[0].begin()
        lang, ext = detect_language(view)
        file_path = view.file_name()
        _log("Suggest at cursor={0} lang={1}".format(cursor, lang["name"]))

        # ── Build prefix (before cursor) and suffix (after cursor) ────────────
        max_lines   = settings.get("max_context_lines", 60)
        n_before    = (max_lines * 2) // 3
        n_after     = max_lines // 3
        total_rows  = view.rowcol(view.size())[0] + 1
        cur_row, _  = view.rowcol(cursor)
        start_row   = max(0, cur_row - n_before)
        end_row     = min(total_rows, cur_row + n_after + 1)
        start_pt    = view.text_point(start_row, 0)
        end_pt      = view.text_point(end_row, 0) if end_row < total_rows else view.size()

        full   = view.substr(sublime.Region(start_pt, end_pt))
        off    = cursor - start_pt
        prefix = full[:off]
        suffix = full[off:]

        extra_context = build_extra_context(view, file_path, ext, settings)
        if extra_context:
            _log("Extra context: {0}".format(extra_context[:120]))

        # ── Cancel previous ───────────────────────────────────────────────────
        vid = view.id()
        old = pending_requests.pop(vid, None)
        if old:
            old[1].set()
            _spinner.stop()

        cancel = threading.Event()
        req_id = (vid, cursor, time.time())
        pending_requests[vid] = (req_id, cancel)

        display_mode = settings.get("display_mode", "phantom")

        def fetch():
            pool         = get_pool()
            max_attempts = max(1, len(pool.model_names()) * 2)
            t_start      = time.time()

            for attempt in range(max_attempts):
                if cancel.is_set():
                    return
                if pending_requests.get(vid, (None,))[0] != req_id:
                    return

                try:
                    slot = pool.acquire()
                except NoModelsError as e:
                    _log_error(str(e))
                    _spinner.stop()
                    sublime.set_timeout(
                        lambda: view.set_status(_STATUS_KEY, "CC: all rate-limited"), 0
                    )
                    return

                label = "[{0} k{1}]".format(slot.display_name, slot.key_index())
                _spinner.start(view, label)
                _log("{0} attempt {1}".format(label, attempt + 1))

                # ── Choose FIM vs chat ────────────────────────────────────────
                headers = {
                    "Content-Type":  "application/json",
                    "Authorization": "Bearer {0}".format(slot.key),
                }

                if slot.use_fim:
                    # Ollama /api/generate — TRUE FIM
                    system  = build_system_for_fim(lang, extra_context)
                    payload = build_fim_payload(slot, prefix, suffix, system)
                    stream_fn = _stream_ollama
                else:
                    # Cloud / OpenAI-compatible chat — FIM-style prompt
                    payload   = build_chat_payload(slot, prefix, suffix, lang, extra_context)
                    stream_fn = _stream_openai

                # ── Token buffer for progressive phantom ──────────────────────
                token_buf  = []
                line_buf   = []
                shown_lines: list = []
                first_token_time  = [None]

                def on_token(tok, _cancel=cancel, _req=req_id):
                    if _cancel.is_set():
                        return
                    if pending_requests.get(vid, (None,))[0] != _req:
                        return
                    if first_token_time[0] is None:
                        first_token_time[0] = time.time()
                        _log("First token: {0:.2f}s".format(
                            first_token_time[0] - t_start
                        ))
                    token_buf.append(tok)

                    # Progressive display only for phantom mode
                    if display_mode != "phantom":
                        return
                    # Accumulate tokens, emit on newline
                    combined = "".join(token_buf)
                    if "\n" in combined:
                        parts = combined.split("\n")
                        for part in parts[:-1]:
                            cleaned = clean_completion(part)
                            if cleaned:
                                shown_lines.append(cleaned)
                        token_buf.clear()
                        token_buf.append(parts[-1])

                        lines_snap = list(shown_lines)
                        if lines_snap:
                            sublime.set_timeout(
                                lambda ls=lines_snap: show_phantom(view, cursor, ls), 0
                            )

                def on_done(full_text, _cancel=cancel, _req=req_id):
                    if _cancel.is_set():
                        return
                    if pending_requests.get(vid, (None,))[0] != _req:
                        return
                    _spinner.stop()

                    elapsed = time.time() - t_start
                    _log("Done in {0:.2f}s — raw: {1!r}".format(
                        elapsed, full_text[:150]
                    ))

                    completion = clean_completion(full_text)
                    if not completion:
                        sublime.set_timeout(
                            lambda: view.erase_status(_STATUS_KEY), 0
                        )
                        return

                    def _display(_comp=completion):
                        if _cancel.is_set():
                            return
                        # Align to current cursor position
                        aligned = align_to_cursor(_comp, view, cursor)
                        if not aligned:
                            clear_phantoms(view)
                            return
                        # Strip overlap with current line content
                        sel2 = view.sel()
                        if not sel2:
                            return
                        cur2    = sel2[0].begin()
                        aligned = strip_line_overlap(aligned, view, cur2)
                        if not aligned:
                            clear_phantoms(view)
                            return
                        # Final lines
                        lines = [l for l in aligned.split("\n")]
                        while lines and lines[0].strip() == "":
                            lines.pop(0)
                        while lines and lines[-1].strip() == "":
                            lines.pop()
                        if not lines:
                            clear_phantoms(view)
                            return

                        view.erase_status(_STATUS_KEY)

                        s2 = sublime.load_settings("CodeContinue.sublime-settings")
                        if s2.get("display_mode", "phantom") == "popup":
                            clear_phantoms(view)
                            show_popup(view, cur2, "\n".join(lines))
                        else:
                            show_phantom(view, cur2, lines)

                    sublime.set_timeout(_display, 0)

                # ── Make the request ──────────────────────────────────────────
                try:
                    req = urllib.request.Request(
                        slot.endpoint,
                        data=json.dumps(payload).encode(),
                        headers=headers,
                    )
                    with urllib.request.urlopen(req, timeout=slot.timeout_s) as resp:
                        stream_fn(resp, on_token, on_done, cancel)
                    pool.report_success(slot)
                    return

                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        _log("429 {0}, rotating key…".format(label))
                        pool.report_429(slot)
                        _spinner.stop()
                        # loop → try next key/model
                    else:
                        _log_error("HTTP {0} {1}".format(e.code, label))
                        pool.report_error(slot)
                        _spinner.stop()
                        sublime.set_timeout(
                            lambda c=e.code: view.set_status(
                                _STATUS_KEY, "CC: HTTP {0}".format(c)
                            ), 0
                        )
                        return

                except urllib.error.URLError as e:
                    _log_error("Network {0}: {1}".format(label, str(e)[:80]))
                    pool.report_error(slot)
                    _spinner.stop()
                    sublime.set_timeout(
                        lambda: view.set_status(_STATUS_KEY, "CC: network error"), 0
                    )
                    return

                except Exception as e:
                    _log_error("Error {0}: {1}".format(label, str(e)[:80]))
                    pool.report_error(slot)
                    _spinner.stop()
                    sublime.set_timeout(
                        lambda: view.set_status(_STATUS_KEY, "CC: error"), 0
                    )
                    return

            _spinner.stop()
            sublime.set_timeout(
                lambda: view.set_status(_STATUS_KEY, "CC: all busy"), 0
            )

        threading.Thread(target=fetch, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────────────
# Accept command — one line at a time (phantom mode)
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueAcceptCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = self.view
        vid  = view.id()
        if vid not in phantoms:
            return
        ps, lines, stored_cursor = phantoms[vid]
        if not lines:
            clear_phantoms(view)
            return
        if len(view.sel()) != 1:
            clear_phantoms(view)
            return

        # Set grace BEFORE insert so on_modified sees it
        accept_grace[vid] = time.time() + 2.0
        suppress_clear.add(vid)
        _cancel_timer(vid)

        try:
            pos   = view.sel()[0].begin()
            first = lines.pop(0)
            rem   = list(lines)

            # Blank first line = newline prefix marker
            ins = "\n" if first.strip() == "" else first + ("\n" if rem else "")
            view.insert(edit, pos, ins)

            new_pos = pos + len(ins)
            view.sel().clear()
            view.sel().add(sublime.Region(new_pos, new_pos))

            if rem:
                ps.update([sublime.Phantom(
                    sublime.Region(new_pos, new_pos),
                    _phantom_html(rem),
                    sublime.LAYOUT_INLINE,
                )])
                phantoms[vid] = (ps, rem, new_pos)
            else:
                clear_phantoms(view)
        finally:
            suppress_clear.discard(vid)


# ──────────────────────────────────────────────────────────────────────────────
# Accept popup command
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueAcceptPopupCommand(sublime_plugin.TextCommand):

    def run(self, edit, text=""):
        view = self.view
        vid  = view.id()

        # Fetch from store if not passed
        if not text:
            entry = _popup_store.pop(vid, None)
            if not entry:
                return
            _, text = entry
        else:
            _popup_store.pop(vid, None)

        view.settings().erase("code_continue_popup_visible")
        view.hide_popup()

        if not text:
            return

        sel = view.sel()
        if not sel:
            return
        pos = sel[0].begin()

        # Prepend newline if line is already complete
        row, _ = view.rowcol(pos)
        line_start = view.text_point(row, 0)
        line_text  = view.substr(sublime.Region(line_start, pos)).rstrip()
        if line_text and line_text[-1] in ";{}":
            text = "\n" + text

        accept_grace[vid] = time.time() + 2.0
        _cancel_timer(vid)
        view.insert(edit, pos, text)

    def is_enabled(self):
        return self.view.id() in _popup_store


# ──────────────────────────────────────────────────────────────────────────────
# Dismiss commands
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueDismissCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        vid = self.view.id()
        old = pending_requests.pop(vid, None)
        if old:
            old[1].set()
        _cancel_timer(vid)
        _spinner.stop()
        clear_phantoms(self.view)
        self.view.erase_status(_STATUS_KEY)

    def is_enabled(self):
        vid = self.view.id()
        return vid in phantoms or vid in pending_requests


class CodeContinueDismissPopupCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        vid = self.view.id()
        _popup_store.pop(vid, None)
        self.view.settings().erase("code_continue_popup_visible")
        self.view.hide_popup()

    def is_enabled(self):
        return self.view.id() in _popup_store