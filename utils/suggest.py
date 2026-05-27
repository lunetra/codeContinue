"""suggest.py — CodeContinue inline code completion engine.

Features:
  - Streaming: phantom updates line-by-line as tokens arrive
  - Smart auto-trigger: fires only after N chars on current line + idle delay
    (never on Enter alone, never when line is empty)
  - Animated spinner in status bar (zzz_ prefix → appears at end of bar)
    shows model name + key index (k1, k2 …)
  - Comment/explanation stripping from model output
  - Newline prefix: if cursor sits after a complete statement, suggestion
    is displayed starting on the next line (no inline concatenation)
  - Response alignment: strips already-typed prefix from a late response
  - ESC to dismiss + cancel in-flight request
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
        )[:25]
    except OSError:
        return []


def build_extra_context(view, file_path, ext, settings):
    parts = []
    siblings = list_sibling_modules(file_path, ext)
    if siblings:
        parts.append(
            "Other {0} files in same directory: {1}".format(ext, ", ".join(siblings))
        )
    if ext == "rs":
        base = os.path.basename(file_path or "")
        if base in ("mod.rs", "lib.rs"):
            parts.append(
                "This is a Rust module file ({0}). "
                "It declares submodules with `mod X;` and re-exports with "
                "`pub use X::something;`. Use ONLY the filenames listed above "
                "as module names. Do not invent names that are not in the list."
                .format(base)
            )
        if settings.get("index_neighboring_files", False) and file_path:
            view_content = view.substr(sublime.Region(0, view.size()))
            sym_ctx = get_symbol_context(
                file_path, view_content, index_neighbors=True, max_chars=300,
            )
            if sym_ctx:
                parts.append(sym_ctx)
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Output cleaning
# ──────────────────────────────────────────────────────────────────────────────

_SPECIAL_TOKEN_RE = re.compile(
    r"<\|(?:im_start|im_end|endoftext|begin_of_text|end_of_text|"
    r"eot_id|start_header_id|end_header_id|fim_prefix|fim_middle|"
    r"fim_suffix|file_separator|user|assistant|system)\|>"
    r"|\[(?:END_OF_TEXT|/?INST)\]"
)

# Lines that are pure comments / explanations — strip them
_COMMENT_LINE_RE = re.compile(
    r"^\s*(?:"
    r"//.*"           # Rust/JS/TS/Go single-line comment
    r"|#.*"           # Python/Bash/TOML comment
    r"|/\*.*\*/"      # Inline block comment
    r"|--.*"          # SQL comment
    r")\s*$"
)


def _strip_comment_lines(text):
    """Remove lines that are purely comments / explanations."""
    lines = text.split("\n")
    filtered = [l for l in lines if not _COMMENT_LINE_RE.match(l)]
    # Drop leading/trailing blank lines that remain after stripping
    while filtered and filtered[0].strip() == "":
        filtered.pop(0)
    while filtered and filtered[-1].strip() == "":
        filtered.pop()
    return "\n".join(filtered)


def clean_completion(text):
    """Strip fences, special tokens, comment-only lines, and explanations."""
    if not text:
        return ""
    # Strip leading markdown fence
    text = re.sub(r"^\s*```[a-zA-Z_+\-]*\s*\n?", "", text)
    # Drop everything after a closing fence (trailing explanation)
    if "```" in text:
        text = text.split("```", 1)[0]
    # Strip special LLM tokens
    text = _SPECIAL_TOKEN_RE.sub("", text)
    # Remove blank lines top/bottom
    lines = text.split("\n")
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    text = "\n".join(lines)
    # Remove pure comment lines
    text = _strip_comment_lines(text)
    return text


# ──────────────────────────────────────────────────────────────────────────────
# Newline prefix detection
# ──────────────────────────────────────────────────────────────────────────────

# Characters that indicate a complete statement / line
_COMPLETE_STMT_ENDS = set(";{}")


def _cursor_is_after_complete_line(view, cursor):
    """
    Return True if the cursor sits at the end of a line that looks complete
    (ends with ; { } etc.), meaning the suggestion should start on the NEXT line.
    """
    row, col = view.rowcol(cursor)
    line_start = view.text_point(row, 0)
    line_text  = view.substr(sublime.Region(line_start, cursor)).rstrip()
    if not line_text:
        return False
    return line_text[-1] in _COMPLETE_STMT_ENDS


# ──────────────────────────────────────────────────────────────────────────────
# Response alignment (trim already-typed prefix from late responses)
# ──────────────────────────────────────────────────────────────────────────────

def align_completion(completion, typed_since_request):
    if not typed_since_request:
        return completion
    typed = typed_since_request.rstrip()
    if not typed:
        return completion
    # Case A: completion starts with what was typed → strip it
    if completion.startswith(typed):
        rest = completion[len(typed):]
        if rest.startswith("\n"):
            rest = rest[1:]
        return rest.strip("\n")
    # Case B: typed text found inside completion → trim from there
    idx = completion.find(typed)
    if idx != -1:
        rest = completion[idx + len(typed):]
        return rest.strip("\n")
    # Case C: completely incompatible (typed ≥10 chars, not in completion)
    if len(typed) >= 10 and typed not in completion:
        _log("Discarding stale completion (typed={0!r})".format(typed[:40]))
        return ""
    return completion


# ──────────────────────────────────────────────────────────────────────────────
# Phantom display  (multi-line via <br>)
# ──────────────────────────────────────────────────────────────────────────────

def _lines_to_html(lines):
    escaped = [html.escape(l).replace(" ", "&nbsp;") for l in lines]
    return "<br>".join(escaped)


def _make_phantom_html(lines):
    return (
        '<body id="cc-phantom">'
        '<span style="color:#6e7681;font-style:italic;">'
        '{0}'
        '</span>'
        '</body>'
    ).format(_lines_to_html(lines))


def show_phantom(view, cursor, lines):
    """Display (or update) the inline ghost-text phantom."""
    clear_phantoms(view)
    if not lines:
        return
    ps  = sublime.PhantomSet(view, "cc_suggest")
    ph  = sublime.Phantom(
        sublime.Region(cursor, cursor),
        _make_phantom_html(lines),
        sublime.LAYOUT_INLINE,
    )
    ps.update([ph])
    phantoms[view.id()] = (ps, list(lines), cursor)
    view.settings().set("code_continue_visible", True)


def update_phantom_lines(view, extra_lines):
    """Append more lines to an existing phantom (streaming update)."""
    vid = view.id()
    if vid not in phantoms:
        return
    ps, current_lines, cursor = phantoms[vid]
    current_lines.extend(extra_lines)
    ph = sublime.Phantom(
        sublime.Region(cursor, cursor),
        _make_phantom_html(current_lines),
        sublime.LAYOUT_INLINE,
    )
    ps.update([ph])
    phantoms[vid] = (ps, current_lines, cursor)


def clear_phantoms(view):
    vid = view.id()
    if vid in phantoms:
        phantoms[vid][0].update([])
        del phantoms[vid]
    view.settings().erase("code_continue_visible")


# ──────────────────────────────────────────────────────────────────────────────
# Animated status bar  (key "zzz_cc" → sorts last in the bar)
# ──────────────────────────────────────────────────────────────────────────────

_STATUS_KEY = "zzz_cc"
_SPINNER    = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class _Spinner:
    """Animates the status bar while a request is in flight."""

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
            sublime.set_timeout(
                lambda: view.erase_status(_STATUS_KEY), 0
            )

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
                lambda: view.set_status(
                    _STATUS_KEY, "{0} {1}".format(spin, label)
                ), 0
            )
        with self._lock:
            if self._active:
                self._timer = threading.Timer(0.1, self._tick)
                self._timer.start()


# ──────────────────────────────────────────────────────────────────────────────
# Plugin state
# ──────────────────────────────────────────────────────────────────────────────

phantoms         = {}   # view.id() -> (PhantomSet, [lines], cursor)
pending_requests = {}   # view.id() -> (request_id, cancel_event)
suppress_clear   = set()
accept_grace     = {}   # view.id() -> float  (also blocks auto-trigger)
_auto_timers     = {}   # view.id() -> threading.Timer
_last_cmd        = {}   # view.id() -> (command_name, args)  set by on_text_command
_spinner         = _Spinner()


# ──────────────────────────────────────────────────────────────────────────────
# Settings / pool
# ──────────────────────────────────────────────────────────────────────────────

def _reload_pool():
    settings = sublime.load_settings("CodeContinue.sublime-settings")
    ok = load_pool_from_settings(settings)
    if ok:
        _log("Pool: {0}".format(get_pool().model_names()))
    return ok


# ──────────────────────────────────────────────────────────────────────────────
# SSE streaming parser
# ──────────────────────────────────────────────────────────────────────────────

def _stream_completion(resp, on_line, on_done, cancel_event):
    """
    Parse an OpenAI SSE stream.
    Calls on_line(text) for each complete line of code received.
    Calls on_done(full_text) when stream ends.
    Respects cancel_event.

    on_line and on_done are called from the background thread —
    callers must use sublime.set_timeout for UI updates.
    """
    buffer    = ""
    full_text = ""

    # readline() reads one SSE line at a time — required for true streaming.
    # for-loop over resp reads arbitrary HTTP chunks which may bundle many events.
    while True:
        if cancel_event.is_set():
            return
        raw = resp.readline()
        if not raw:
            break  # connection closed

        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            chunk   = json.loads(data)
            delta   = chunk["choices"][0].get("delta", {})
            content = delta.get("content") or ""
        except (KeyError, IndexError, json.JSONDecodeError):
            continue

        buffer    += content
        full_text += content

        # Emit each complete code line as it arrives
        while "\n" in buffer:
            nl_pos   = buffer.index("\n")
            complete = buffer[:nl_pos]
            buffer   = buffer[nl_pos + 1:]
            cleaned  = clean_completion(complete)
            if cleaned:
                on_line(cleaned)

    # Emit any remaining content (last line without trailing newline)
    if buffer.strip():
        cleaned = clean_completion(buffer)
        if cleaned:
            on_line(cleaned)

    on_done(clean_completion(full_text))


# ──────────────────────────────────────────────────────────────────────────────
# Event listener
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueListener(sublime_plugin.EventListener):

    def on_text_command(self, view, command_name, args):
        """Track which command caused the next on_modified."""
        _last_cmd[view.id()] = (command_name, args or {})
        return None

    def on_modified(self, view):
        vid = view.id()
        cmd, args = _last_cmd.pop(vid, (None, {}))

        # Clear phantom on any edit (unless Tab-accept is in progress)
        if vid in phantoms:
            if vid not in suppress_clear and time.time() >= accept_grace.get(vid, 0):
                clear_phantoms(view)

        settings = sublime.load_settings("CodeContinue.sublime-settings")
        if not settings.get("auto_trigger", True):
            return

        # ── Only trigger on a single printable character being typed ──────────
        # Ignore: backspace, delete, enter, paste, Tab-accept, undo, redo, etc.
        if cmd != "insert":
            return
        typed_char = args.get("characters", "")
        # Must be exactly one non-whitespace, non-newline character
        if len(typed_char) != 1 or typed_char in " \t\n\r":
            return

        # ── Grace period after Tab-accept: don't re-trigger immediately ───────
        if time.time() < accept_grace.get(vid, 0):
            return

        # ── Language gate ─────────────────────────────────────────────────────
        trigger_langs = settings.get("trigger_language", [])
        _, ext = detect_language(view)
        syntax = view.syntax()
        syntax_name = syntax.name.lower() if syntax else ""
        if trigger_langs and not (
            ext in [t.lower() for t in trigger_langs]
            or any(t.lower() in syntax_name for t in trigger_langs)
        ):
            return

        # ── Guard: current line must have enough content ──────────────────────
        sel = view.sel()
        if not sel:
            return
        cursor = sel[0].begin()
        row, _col = view.rowcol(cursor)
        line_start = view.text_point(row, 0)
        line_text  = view.substr(sublime.Region(line_start, cursor))

        min_chars = settings.get("auto_trigger_min_chars", 4)
        if len(line_text.strip()) < min_chars:
            t = _auto_timers.pop(vid, None)
            if t:
                t.cancel()
            return

        # ── Guard: don't trigger when line already ends a complete statement ───
        stripped = line_text.rstrip()
        if stripped and stripped[-1] in ";{}":
            t = _auto_timers.pop(vid, None)
            if t:
                t.cancel()
            return

        # ── Debounce ──────────────────────────────────────────────────────────
        t = _auto_timers.pop(vid, None)
        if t:
            t.cancel()

        delay = settings.get("auto_trigger_delay", 0.9)

        def _fire():
            sublime.set_timeout(
                lambda: view.run_command("code_continue_suggest"), 0
            )

        timer = threading.Timer(delay, _fire)
        _auto_timers[vid] = timer
        timer.start()

    def on_post_save(self, view):
        fp = view.file_name()
        if fp:
            on_file_saved(fp)
        vid = view.id()
        t = _auto_timers.pop(vid, None)
        if t:
            t.cancel()


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
        _log("Language: {0} (ext={1})".format(lang["name"], ext))

        # ── Decide if suggestion should start on next line ────────────────────
        needs_newline_prefix = _cursor_is_after_complete_line(view, cursor)

        # ── Context window ────────────────────────────────────────────────────
        max_lines  = settings.get("max_context_lines", 60)
        n_before   = (max_lines * 2) // 3
        n_after    = max_lines // 3
        total_rows = view.rowcol(view.size())[0] + 1
        cur_row, _ = view.rowcol(cursor)
        start_row  = max(0, cur_row - n_before)
        end_row    = min(total_rows, cur_row + n_after + 1)
        start_pt   = view.text_point(start_row, 0)
        end_pt     = view.text_point(end_row, 0) if end_row < total_rows else view.size()

        full        = view.substr(sublime.Region(start_pt, end_pt))
        off         = cursor - start_pt
        code_before = full[:off]
        code_after  = full[off:]

        extra_context = build_extra_context(view, file_path, ext, settings)
        if extra_context:
            _log("Context: {0}".format(extra_context[:200]))

        messages = _build_messages(code_before, code_after, lang, extra_context)

        # ── Cancel previous request ───────────────────────────────────────────
        vid = view.id()
        old = pending_requests.get(vid)
        if old:
            old[1].set()
            _spinner.stop()

        cancel     = threading.Event()
        req_id     = (vid, cursor, time.time())
        req_cursor = cursor
        pending_requests[vid] = (req_id, cancel)

        def fetch():
            pool         = get_pool()
            max_attempts = max(1, len(pool.model_names()) * 2)

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

                # Key index (1-based) for display
                key_idx = slot.model_state.keys.index(slot.key_state) + 1
                label   = "[{0} k{1}]".format(slot.display_name, key_idx)
                _spinner.start(view, label)
                _log("Using {0} attempt {1}".format(label, attempt + 1))

                headers = {
                    "Content-Type":  "application/json",
                    "Authorization": "Bearer {0}".format(slot.key),
                }
                payload = {
                    "model":       slot.model_id,
                    "messages":    messages,
                    "max_tokens":  slot.max_tokens,
                    "temperature": slot.temperature,
                    "top_p":       slot.top_p,
                    "stream":      True,   # always stream
                }

                try:
                    req = urllib.request.Request(
                        slot.endpoint,
                        data=json.dumps(payload).encode(),
                        headers=headers,
                    )

                    t_start   = time.time()
                    first_tok = [None]   # mutable for closure
                    collected = []       # list of cleaned lines received so far

                    with urllib.request.urlopen(req, timeout=slot.timeout_s) as resp:

                        def on_line(text, needs_nl=needs_newline_prefix):
                            """Called from background thread for each complete line."""
                            if cancel.is_set():
                                return
                            if pending_requests.get(vid, (None,))[0] != req_id:
                                return

                            if first_tok[0] is None:
                                first_tok[0] = time.time()
                                _log("First token in {0:.2f}s".format(
                                    first_tok[0] - t_start
                                ))

                            collected.append(text)

                            # Progressive phantom update
                            lines_to_show = list(collected)
                            cur_cursor    = req_cursor  # display at original cursor

                            def _update(lines=lines_to_show, nl=needs_nl, cc=cur_cursor):
                                if cancel.is_set():
                                    return
                                if nl:
                                    # Insert a visual newline: show blank first line
                                    display = [""] + lines
                                else:
                                    display = lines
                                show_phantom(view, cc, display)

                            sublime.set_timeout(_update, 0)

                        def on_done(full_text):
                            """Called from background thread when stream ends."""
                            if cancel.is_set():
                                return
                            if pending_requests.get(vid, (None,))[0] != req_id:
                                return

                            elapsed = time.time() - t_start
                            _log("Stream done in {0:.2f}s — full: {1!r}".format(
                                elapsed, full_text[:120]
                            ))
                            _spinner.stop()

                            if not full_text.strip():
                                sublime.set_timeout(
                                    lambda: view.set_status(_STATUS_KEY, "CC: no suggestion"), 0
                                )
                                return

                            # Alignment check (user may have typed while waiting)
                            def _align_and_show(ft=full_text, req_cur=req_cursor,
                                                nl=needs_newline_prefix):
                                if cancel.is_set():
                                    return
                                current_sel = view.sel()
                                if len(current_sel) != 1:
                                    clear_phantoms(view)
                                    return
                                current_cursor = current_sel[0].begin()
                                if current_cursor > req_cur:
                                    typed = view.substr(
                                        sublime.Region(req_cur, current_cursor)
                                    )
                                else:
                                    typed = ""
                                aligned = align_completion(ft, typed)
                                if not aligned:
                                    clear_phantoms(view)
                                    return
                                # Clean up aligned text
                                lines = [l for l in aligned.split("\n")]
                                while lines and lines[0].strip() == "":
                                    lines.pop(0)
                                while lines and lines[-1].strip() == "":
                                    lines.pop()
                                if not lines:
                                    clear_phantoms(view)
                                    return
                                # ── Choose display mode ───────────────────────
                                s = sublime.load_settings("CodeContinue.sublime-settings")
                                mode = s.get("display_mode", "phantom")
                                if mode == "popup":
                                    clear_phantoms(view)
                                    show_popup_suggestion(
                                        view, current_cursor,
                                        "\n".join(lines)
                                    )
                                else:
                                    display = ([""] + lines) if nl else lines
                                    show_phantom(view, current_cursor, display)

                            sublime.set_timeout(_align_and_show, 0)

                        _stream_completion(resp, on_line, on_done, cancel)

                    pool.report_success(slot)
                    return  # ← done

                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        _log("429 [{0}], rotating …".format(slot.display_name))
                        pool.report_429(slot)
                        _spinner.stop()
                        # loop → try next slot
                    else:
                        _log_error("HTTP {0} [{1}]".format(e.code, slot.display_name))
                        pool.report_error(slot)
                        _spinner.stop()
                        sublime.set_timeout(
                            lambda c=e.code: view.set_status(
                                _STATUS_KEY, "CC: HTTP {0}".format(c)
                            ), 0
                        )
                        return

                except urllib.error.URLError as e:
                    _log_error("Network [{0}]: {1}".format(
                        slot.display_name, str(e)[:80]
                    ))
                    pool.report_error(slot)
                    _spinner.stop()
                    sublime.set_timeout(
                        lambda: view.set_status(_STATUS_KEY, "CC: network error"), 0
                    )
                    return

                except Exception as e:
                    _log_error("Error [{0}]: {1}".format(
                        slot.display_name, str(e)[:80]
                    ))
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


def _build_messages(code_before, code_after, lang, extra_context):
    name  = lang["name"]
    fence = lang["fence"]
    system = (
        "You are a {name} code autocomplete engine. "
        "Your output is inserted directly into the editor.\n\n"
        "Rules:\n"
        "1. Output ONLY raw code — no markdown fences, no backticks.\n"
        "2. NO comments, NO explanations, NO docstrings. Only code lines.\n"
        "3. Match the existing indentation and code style exactly.\n"
        "4. Output 1-6 lines that naturally continue the pattern shown.\n"
        "5. Use ONLY names, types, and modules already visible in the context.\n"
        "6. Never repeat existing code. Never invent names not in the context.\n"
        "7. Stop when the immediate logical unit is complete."
    ).format(name=name)
    if extra_context:
        system += "\n\nProject context (reference only):\n" + extra_context

    if code_after.strip():
        user = (
            "Complete the gap at <CURSOR/> in this {name} code.\n"
            "Output ONLY raw code:\n\n"
            "```{fence}\n{before}<CURSOR/>{after}\n```"
        ).format(name=name, fence=fence, before=code_before, after=code_after[:400])
    else:
        user = (
            "Continue this {name} code. Output ONLY the raw continuation:\n\n"
            "```{fence}\n{before}\n```"
        ).format(name=name, fence=fence, before=code_before)

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Accept command  (Tab — one line at a time)
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

        suppress_clear.add(vid)
        try:
            pos   = view.sel()[0].begin()
            first = lines.pop(0)
            rem   = list(lines)

            # If first line is blank (newline prefix marker), insert a newline
            if first.strip() == "":
                ins = "\n"
            else:
                ins = first + ("\n" if rem else "")

            view.insert(edit, pos, ins)
            new_pos = pos + len(ins)
            view.sel().clear()
            view.sel().add(sublime.Region(new_pos, new_pos))

            if rem:
                ph = sublime.Phantom(
                    sublime.Region(new_pos, new_pos),
                    _make_phantom_html(rem),
                    sublime.LAYOUT_INLINE,
                )
                ps.update([ph])
                phantoms[vid] = (ps, rem, new_pos)
            else:
                clear_phantoms(view)
        finally:
            accept_grace[vid] = time.time() + 2.0  # 2s: prevents re-trigger after Tab-accept
            suppress_clear.discard(vid)



# ──────────────────────────────────────────────────────────────────────────────
# Popup mode  (view.show_popup with Accept / Dismiss buttons)
# ──────────────────────────────────────────────────────────────────────────────

_popup_suggestion = {}   # view.id() -> (cursor, full_text)


def show_popup_suggestion(view, cursor, suggestion):
    """Show the full suggestion in a popup panel with Accept/Dismiss buttons."""
    _popup_suggestion[view.id()] = (cursor, suggestion)
    lines   = suggestion.split("\n")
    code_html = "<br>".join(
        html.escape(l).replace(" ", "&nbsp;") for l in lines
    )
    content = (
        '<body style="margin:0;padding:0;">'
        '<div style="padding:6px 8px;font-family:monospace;font-size:0.95em;'
        'color:#cdd9e5;background:#22272e;border-bottom:1px solid #444c56;">'
        '{code}'
        '</div>'
        '<div style="padding:4px 8px;background:#2d333b;">'
        '<a href="accept" style="color:#57ab5a;text-decoration:none;">✓ Accept</a>'
        '&nbsp;&nbsp;&nbsp;'
        '<a href="dismiss" style="color:#e5534b;text-decoration:none;">✗ Dismiss</a>'
        '</div>'
        '</body>'
    ).format(code=code_html)
    view.show_popup(
        content,
        flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
        location=cursor,
        max_width=700,
        max_height=350,
        on_navigate=lambda href: _popup_navigate(view, href),
        on_hide=lambda: _popup_suggestion.pop(view.id(), None),
    )


def _popup_navigate(view, href):
    vid = view.id()
    if href == "accept":
        entry = _popup_suggestion.pop(vid, None)
        if entry:
            _cursor, text = entry
            view.run_command("code_continue_accept_popup", {"text": text})
    else:
        _popup_suggestion.pop(vid, None)
    view.hide_popup()


class CodeContinueAcceptPopupCommand(sublime_plugin.TextCommand):
    """Insert the full popup suggestion at the cursor position."""
    def run(self, edit, text=""):
        view = self.view
        sel  = view.sel()
        if not sel:
            return
        pos = sel[0].begin()
        # If cursor is after a complete statement, prepend newline
        row, _ = view.rowcol(pos)
        line_start = view.text_point(row, 0)
        line_text  = view.substr(sublime.Region(line_start, pos)).rstrip()
        if line_text and line_text[-1] in ";{}":
            text = "\n" + text
        view.insert(edit, pos, text)


# ──────────────────────────────────────────────────────────────────────────────
# Dismiss command  (ESC)
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueDismissCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        vid = self.view.id()
        old = pending_requests.get(vid)
        if old:
            old[1].set()
        t = _auto_timers.pop(vid, None)
        if t:
            t.cancel()
        _spinner.stop()
        clear_phantoms(self.view)

    def is_enabled(self):
        vid = self.view.id()
        return vid in phantoms or vid in pending_requests