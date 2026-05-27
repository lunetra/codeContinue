"""suggest.py — CodeContinue inline code completion engine.

Improvements in this version:
  - Multi-line phantom with proper <br> HTML rendering
  - Debounced auto-trigger: fires after N seconds of typing pause (configurable)
  - Response alignment: when response arrives, strip the part the user already
    typed since the request was made — so suggestion always starts at cursor
  - Compatibility check: if suggestion is incompatible with what was typed, discard
  - Smart mod.rs context: detects module files and adds pattern hint to prompt
  - ESC dismiss via keybinding (CodeContinueDismissCommand)
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
    """Return sorted stems of sibling files with the same extension."""
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


def _is_mod_rs(file_path):
    """Return True if this is a Rust module index file (mod.rs or lib.rs)."""
    if not file_path:
        return False
    base = os.path.basename(file_path)
    return base in ("mod.rs", "lib.rs")


def build_extra_context(view, file_path, ext, settings):
    """Build the extra project context injected into the system prompt."""
    parts = []

    siblings = list_sibling_modules(file_path, ext)
    if siblings:
        parts.append(
            "Other {0} files in same directory: {1}".format(ext, ", ".join(siblings))
        )

    # Rust-specific context
    if ext == "rs":
        if _is_mod_rs(file_path):
            # Help the model understand what mod.rs is for
            parts.append(
                "This is a Rust module file (mod.rs / lib.rs). "
                "Its job is to declare submodules with `mod X;` and "
                "optionally re-export items with `pub use X::something;`. "
                "Use ONLY the filenames listed above as module names."
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


def clean_completion(text):
    """Strip markdown fences, special tokens, and trailing explanations."""
    if not text:
        return ""
    text = re.sub(r"^\s*```[a-zA-Z_+\-]*\s*\n?", "", text)
    if "```" in text:
        text = text.split("```", 1)[0]
    text = _SPECIAL_TOKEN_RE.sub("", text)
    lines = text.split("\n")
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Response alignment
# ──────────────────────────────────────────────────────────────────────────────

def align_completion(completion, typed_since_request):
    """
    When a response arrives after the user has continued typing, align it.

    Cases:
      A) User typed nothing extra → return completion as-is
      B) Completion starts with what user typed → strip that prefix
      C) Completion is compatible (starts at the right place) → trim
      D) Completion is incompatible → return "" (discard)

    "Compatible" means the completion's first non-whitespace content
    matches or overlaps with where the user's cursor now sits.
    """
    if not typed_since_request:
        return completion

    typed = typed_since_request

    # Normalise: strip trailing whitespace/newlines from typed
    typed_stripped = typed.rstrip()
    if not typed_stripped:
        return completion

    # Case B: completion begins with what was typed (common case)
    if completion.startswith(typed_stripped):
        rest = completion[len(typed_stripped):]
        # Strip a single leading newline if the typed text ended mid-line
        if rest.startswith("\n"):
            rest = rest[1:]
        result = rest.lstrip("\n") if not typed_stripped.endswith("\n") else rest
        return result.rstrip("\n") if result else ""

    # Case C: find where typed_stripped first appears in completion
    # (user typed something the model also predicted)
    idx = completion.find(typed_stripped)
    if idx != -1:
        rest = completion[idx + len(typed_stripped):]
        return rest.lstrip("\n").rstrip("\n")

    # Case D: if the user typed something substantive (>3 chars) that doesn't
    # appear anywhere in the completion, the suggestion is stale — discard.
    if len(typed_stripped) >= 10 and typed_stripped not in completion:
        _log("Discarding stale completion (typed={0!r})".format(
            typed_stripped[:40]
        ))
        return ""

    # Minimal typed text (newline, 1-3 chars) — keep completion as-is
    return completion


# ──────────────────────────────────────────────────────────────────────────────
# Prompt building
# ──────────────────────────────────────────────────────────────────────────────

def build_messages(code_before, code_after, lang, extra_context):
    name  = lang["name"]
    fence = lang["fence"]

    system = (
        "You are a {name} code autocomplete engine. "
        "Your output is inserted directly into the editor at the cursor.\n\n"
        "Rules:\n"
        "1. Output ONLY raw code — no markdown fences, no backticks, no explanations.\n"
        "2. Match the existing indentation and code style exactly.\n"
        "3. Output 1-5 lines that naturally continue the pattern shown.\n"
        "4. Use ONLY names, types, and modules already visible in the context.\n"
        "5. Never repeat code that already exists. Never invent new names.\n"
        "6. Stop when the immediate logical unit is complete."
    ).format(name=name)

    if extra_context:
        system += "\n\nProject context (reference only):\n" + extra_context

    if code_after.strip():
        user = (
            "Complete the gap at <CURSOR/> in this {name} code.\n"
            "Output ONLY the raw code that goes at <CURSOR/>:\n\n"
            "```{fence}\n{before}<CURSOR/>{after}\n```"
        ).format(name=name, fence=fence, before=code_before, after=code_after[:400])
    else:
        user = (
            "Continue this {name} code. "
            "Output ONLY the raw continuation (no repeating existing code):\n\n"
            "```{fence}\n{before}\n```"
        ).format(name=name, fence=fence, before=code_before)

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Plugin state
# ──────────────────────────────────────────────────────────────────────────────

phantoms          = {}   # view.id() -> (PhantomSet, [lines], common_prefix)
pending_requests  = {}   # view.id() -> (request_id, cancel_event)
suppress_clear    = set()
accept_grace      = {}   # view.id() -> float
_auto_timers      = {}   # view.id() -> threading.Timer  (debounce)


# ──────────────────────────────────────────────────────────────────────────────
# Phantom display  — multi-line with <br>
# ──────────────────────────────────────────────────────────────────────────────

def _lines_to_html(lines):
    """Convert a list of code lines to inline HTML with proper line breaks."""
    escaped = [html.escape(l).replace(" ", "&nbsp;") for l in lines]
    return "<br>".join(escaped)


def show_phantom(view, cursor, suggestion):
    clear_phantoms(view)
    ps    = sublime.PhantomSet(view)
    lines = suggestion.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    while lines and lines[0].strip() == "":
        lines.pop(0)
    if not lines:
        return

    norm, prefix = strip_common_indent(lines)

    content = (
        '<body id="cc-phantom">'
        '<span style="color:#888888;font-style:italic;">'
        '{0}'
        '</span>'
        '</body>'
    ).format(_lines_to_html(norm))

    ph = sublime.Phantom(
        sublime.Region(cursor, cursor),
        content,
        sublime.LAYOUT_INLINE,
    )
    ps.update([ph])
    phantoms[view.id()] = (ps, norm, prefix)
    view.set_status("code_continue_visible", "true")


def clear_phantoms(view):
    vid = view.id()
    if vid in phantoms:
        phantoms[vid][0].update([])
        del phantoms[vid]
    view.erase_status("code_continue_visible")


def _set_status(view, msg):
    view.set_status("code_continue", msg)


def _clear_status(view):
    view.erase_status("code_continue")


# ──────────────────────────────────────────────────────────────────────────────
# Settings / pool helpers
# ──────────────────────────────────────────────────────────────────────────────

def _reload_pool():
    settings = sublime.load_settings("CodeContinue.sublime-settings")
    ok = load_pool_from_settings(settings)
    if ok:
        _log("Pool: {0}".format(get_pool().model_names()))
    else:
        _log_error("No models configured.")
    return ok


# ──────────────────────────────────────────────────────────────────────────────
# Event listener
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueListener(sublime_plugin.EventListener):

    def on_modified(self, view):
        vid = view.id()

        # Clear phantom on any edit (unless Tab-accept is in progress)
        if vid in phantoms:
            if vid not in suppress_clear and time.time() >= accept_grace.get(vid, 0):
                clear_phantoms(view)

        # ── Debounced auto-trigger ────────────────────────────────────────────
        settings = sublime.load_settings("CodeContinue.sublime-settings")
        if not settings.get("auto_trigger", True):
            return

        # Check language is enabled
        trigger_langs = settings.get("trigger_language", [])
        _, ext = detect_language(view)
        syntax = view.syntax()
        syntax_name = syntax.name.lower() if syntax else ""
        if trigger_langs and not (
            ext in [t.lower() for t in trigger_langs]
            or any(t.lower() in syntax_name for t in trigger_langs)
        ):
            return

        # Cancel previous timer
        old_timer = _auto_timers.get(vid)
        if old_timer:
            old_timer.cancel()

        delay = settings.get("auto_trigger_delay", 0.9)

        def _fire():
            # Run on the Sublime main thread
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
        # Cancel any pending auto-trigger on save
        vid = view.id()
        t = _auto_timers.pop(vid, None)
        if t:
            t.cancel()

    def on_text_command(self, view, command_name, args):
        """Intercept Enter to optionally trigger (legacy Enter-based mode)."""
        if command_name != "insert" or not args or args.get("characters") != "\n":
            return None

        settings = sublime.load_settings("CodeContinue.sublime-settings")

        # If auto_trigger is on, Enter-based trigger is redundant — skip
        if settings.get("auto_trigger", True):
            return None

        trigger_langs = settings.get("trigger_language", [])
        _, ext = detect_language(view)
        syntax = view.syntax()
        syntax_name = syntax.name.lower() if syntax else ""
        if trigger_langs and not (
            ext in [t.lower() for t in trigger_langs]
            or any(t.lower() in syntax_name for t in trigger_langs)
        ):
            return None

        vid = view.id()
        if vid in phantoms:
            return None

        sublime.set_timeout(lambda: view.run_command("code_continue_suggest"), 50)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Suggest command
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueSuggestCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view     = self.view
        settings = sublime.load_settings("CodeContinue.sublime-settings")

        if not _reload_pool():
            _set_status(view, "CC: no models configured")
            sublime.set_timeout(lambda: show_endpoint_config_panel(view), 100)
            return

        sel = view.sel()
        if len(sel) != 1:
            return

        cursor    = sel[0].begin()
        lang, ext = detect_language(view)
        file_path = view.file_name()
        _log("Language: {0} (ext={1})".format(lang["name"], ext))

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

        # ── Extra context ─────────────────────────────────────────────────────
        extra_context = build_extra_context(view, file_path, ext, settings)
        if extra_context:
            _log("Context: {0}".format(extra_context[:200]))

        messages = build_messages(code_before, code_after, lang, extra_context)

        # ── Cancel previous request ───────────────────────────────────────────
        vid = view.id()
        old = pending_requests.get(vid)
        if old:
            old[1].set()

        cancel = threading.Event()
        req_id = (vid, cursor, time.time())
        pending_requests[vid] = (req_id, cancel)

        # ── Track cursor position at request time (for alignment) ─────────────
        request_cursor = cursor   # captured now; user may move cursor while waiting

        def fetch():
            pool = get_pool()
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
                    sublime.set_timeout(
                        lambda: _set_status(view, "CC: all models rate-limited"), 0
                    )
                    return

                _log("[{0}] attempt {1}".format(slot.display_name, attempt + 1))
                sublime.set_timeout(
                    lambda n=slot.display_name: _set_status(
                        view, "CC: [{0}] …".format(n)
                    ), 0
                )

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
                }

                try:
                    req = urllib.request.Request(
                        slot.endpoint,
                        data=json.dumps(payload).encode(),
                        headers=headers,
                    )
                    with urllib.request.urlopen(req, timeout=slot.timeout_s) as resp:
                        body    = json.loads(resp.read())
                        choices = body.get("choices", [])
                        raw     = ""
                        if choices:
                            raw = (
                                choices[0].get("message", {}).get("content", "")
                                or choices[0].get("text", "")
                            )

                    pool.report_success(slot)

                    if cancel.is_set():
                        return
                    if pending_requests.get(vid, (None,))[0] != req_id:
                        return

                    _log("Raw [{0}]: {1!r}".format(slot.display_name, raw[:200]))
                    completion = clean_completion(raw)

                    if not completion:
                        sublime.set_timeout(
                            lambda: _set_status(view, "CC: no suggestion"), 0
                        )
                        return

                    # ── Alignment: what did the user type since request? ───────
                    def _show_aligned(comp=completion, req_cur=request_cursor):
                        current_sel = view.sel()
                        if len(current_sel) != 1:
                            _clear_status(view)
                            return

                        current_cursor = current_sel[0].begin()

                        # Get text typed since request was sent
                        if current_cursor > req_cur:
                            typed_since = view.substr(
                                sublime.Region(req_cur, current_cursor)
                            )
                        else:
                            typed_since = ""

                        aligned = align_completion(comp, typed_since)
                        _log("Aligned (typed={0!r}): {1!r}".format(
                            typed_since[:40], aligned[:80]
                        ))

                        if aligned:
                            show_phantom(view, current_cursor, aligned)
                        _clear_status(view)

                    sublime.set_timeout(_show_aligned, 0)
                    return

                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        _log("429 [{0}], rotating …".format(slot.display_name))
                        pool.report_429(slot)
                    else:
                        _log_error("HTTP {0} [{1}]".format(e.code, slot.display_name))
                        pool.report_error(slot)
                        sublime.set_timeout(
                            lambda c=e.code: _set_status(
                                view, "CC: HTTP {0}".format(c)
                            ), 0
                        )
                        return

                except urllib.error.URLError as e:
                    _log_error("Network [{0}]: {1}".format(
                        slot.display_name, str(e)[:80]
                    ))
                    pool.report_error(slot)
                    sublime.set_timeout(
                        lambda: _set_status(view, "CC: network error"), 0
                    )
                    return

                except Exception as e:
                    _log_error("Error [{0}]: {1}".format(
                        slot.display_name, str(e)[:80]
                    ))
                    pool.report_error(slot)
                    sublime.set_timeout(
                        lambda: _set_status(view, "CC: error — see console"), 0
                    )
                    return

            sublime.set_timeout(
                lambda: _set_status(view, "CC: all models busy"), 0
            )

        threading.Thread(target=fetch, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────────────
# Accept command  (Tab — one line at a time)
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueAcceptCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = self.view
        vid  = view.id()
        if vid not in phantoms:
            return

        ps, remaining, common_prefix = phantoms[vid]
        if not remaining:
            clear_phantoms(view)
            return
        if len(view.sel()) != 1:
            clear_phantoms(view)
            return

        suppress_clear.add(vid)
        try:
            pos   = view.sel()[0].begin()
            first = remaining.pop(0)
            rem   = [common_prefix + l for l in remaining] if common_prefix else remaining
            ins   = first + ("\n" if rem else "")
            view.insert(edit, pos, ins)

            new_pos = pos + len(ins)
            view.sel().clear()
            view.sel().add(sublime.Region(new_pos, new_pos))

            if rem:
                norm, prefix = strip_common_indent(rem)
                ps.update([sublime.Phantom(
                    sublime.Region(new_pos, new_pos),
                    '<body id="cc-phantom">'
                    '<span style="color:#888888;font-style:italic;">{0}</span>'
                    '</body>'.format(_lines_to_html(norm)),
                    sublime.LAYOUT_INLINE,
                )])
                phantoms[vid] = (ps, rem, prefix)
            else:
                clear_phantoms(view)
        finally:
            accept_grace[vid] = time.time() + 0.25
            suppress_clear.discard(vid)


# ──────────────────────────────────────────────────────────────────────────────
# Dismiss command  (ESC)
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueDismissCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = self.view
        vid  = view.id()

        # Cancel in-flight request
        old = pending_requests.get(vid)
        if old:
            old[1].set()

        # Cancel pending auto-trigger
        t = _auto_timers.pop(vid, None)
        if t:
            t.cancel()

        clear_phantoms(view)
        _clear_status(view)

    def is_enabled(self):
        vid = self.view.id()
        return vid in phantoms or vid in pending_requests