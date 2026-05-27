"""suggest.py — CodeContinue inline code completion engine."""

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
# Sibling module listing (same dir, same extension)
# ──────────────────────────────────────────────────────────────────────────────

def list_sibling_modules(file_path, ext):
    if not file_path or not ext or ext == "_default":
        return ""
    d   = os.path.dirname(file_path)
    cur = os.path.basename(file_path)
    try:
        stems = sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(d)
            if f.endswith("." + ext) and f != cur and not f.startswith(".")
        )
        if stems:
            return ", ".join(stems[:25])
    except OSError:
        pass
    return ""


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
    # Strip leading fence (```rust, ```, etc.)
    text = re.sub(r"^\s*```[a-zA-Z_+\-]*\s*\n?", "", text)
    # Drop everything after a closing fence (explanations)
    if "```" in text:
        text = text.split("```", 1)[0]
    # Strip special LLM tokens
    text = _SPECIAL_TOKEN_RE.sub("", text)
    # Remove leading/trailing blank lines, preserve internal indentation
    lines = text.split("\n")
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return "\n".join(lines)


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
        "3. Output 1-4 lines that naturally continue the pattern shown.\n"
        "4. Use only names, types, and modules already visible in the context.\n"
        "5. Never repeat code that already exists. Never invent new abstractions.\n"
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

phantoms         = {}
last_request     = {}
pending_requests = {}
suppress_clear   = set()
accept_grace     = {}


# ──────────────────────────────────────────────────────────────────────────────
# Phantom display
# ──────────────────────────────────────────────────────────────────────────────

def show_phantom(view, cursor, suggestion):
    clear_phantoms(view)
    ps    = sublime.PhantomSet(view)
    lines = suggestion.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    norm, prefix = strip_common_indent(lines)
    preview = "\n".join(norm)
    if not preview:
        return
    ph = sublime.Phantom(
        sublime.Region(cursor, cursor),
        '<span style="color:gray;font-style:italic;">{0}</span>'.format(
            html.escape(preview)
        ),
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
# Settings helpers
# ──────────────────────────────────────────────────────────────────────────────

def _reload_pool():
    settings = sublime.load_settings("CodeContinue.sublime-settings")
    ok = load_pool_from_settings(settings)
    if ok:
        _log("Model pool loaded: {0}".format(get_pool().model_names()))
    else:
        _log_error("No models configured in CodeContinue.sublime-settings")
    return ok


def _is_pool_ready():
    return len(get_pool().model_names()) > 0


# ──────────────────────────────────────────────────────────────────────────────
# Event listener
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueListener(sublime_plugin.EventListener):

    def on_modified(self, view):
        vid = view.id()
        if vid not in phantoms:
            return
        if vid in suppress_clear:
            return
        if time.time() < accept_grace.get(vid, 0):
            return
        clear_phantoms(view)

    def on_post_save(self, view):
        fp = view.file_name()
        if fp:
            on_file_saved(fp)

    def on_text_command(self, view, command_name, args):
        if command_name != "insert" or not args or args.get("characters") != "\n":
            return None

        settings      = sublime.load_settings("CodeContinue.sublime-settings")
        trigger_langs = settings.get("trigger_language", [])
        _, ext        = detect_language(view)
        syntax        = view.syntax()
        syntax_name   = syntax.name.lower() if syntax else ""

        if trigger_langs and not (
            ext in [t.lower() for t in trigger_langs]
            or any(t.lower() in syntax_name for t in trigger_langs)
        ):
            return None

        vid = view.id()
        if vid in phantoms:
            return None

        now = time.time()
        if now - last_request.get(vid, 0) < 1.0:
            return None
        last_request[vid] = now

        sublime.set_timeout(lambda: view.run_command("code_continue_suggest"), 50)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Suggest command
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueSuggestCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view     = self.view
        settings = sublime.load_settings("CodeContinue.sublime-settings")

        # Reload pool from settings on each request so live edits to settings
        # take effect without restarting Sublime.
        if not _reload_pool():
            _set_status(view, "CC: No models configured")
            sublime.set_timeout(lambda: show_endpoint_config_panel(view), 100)
            return

        sel = view.sel()
        if len(sel) != 1:
            return

        cursor    = sel[0].begin()
        lang, ext = detect_language(view)
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
        extra_parts = []
        file_path   = view.file_name()

        siblings = list_sibling_modules(file_path, ext)
        if siblings:
            extra_parts.append(
                "Other {0} files in same directory: {1}".format(ext, siblings)
            )

        if ext == "rs" and settings.get("index_neighboring_files", False):
            if file_path:
                view_content = view.substr(sublime.Region(0, view.size()))
                sym_ctx = get_symbol_context(
                    file_path, view_content,
                    index_neighbors=True, max_chars=300,
                )
                if sym_ctx:
                    extra_parts.append(sym_ctx)

        extra_context = "\n".join(extra_parts)
        if extra_context:
            _log("Extra context ({0} chars):\n{1}".format(
                len(extra_context), extra_context[:300]
            ))

        messages = build_messages(code_before, code_after, lang, extra_context)

        # ── Cancel previous request ───────────────────────────────────────────
        vid = view.id()
        old = pending_requests.get(vid)
        if old:
            old[1].set()

        cancel = threading.Event()
        req_id = (vid, cursor, time.time())
        pending_requests[vid] = (req_id, cancel)

        def fetch():
            pool = get_pool()

            # Try models in rotation. If a 429 happens, mark the key and retry
            # with the next available model/key — up to len(pool) attempts.
            max_attempts = max(1, len(pool.model_names()) * 2)

            for attempt in range(max_attempts):
                if cancel.is_set():
                    return
                if pending_requests.get(vid, (None,))[0] != req_id:
                    return

                # ── Acquire a model slot ──────────────────────────────────────
                try:
                    slot = pool.acquire()
                except NoModelsError as e:
                    _log_error(str(e))
                    sublime.set_timeout(
                        lambda: _set_status(view, "CC: all models rate-limited"), 0
                    )
                    return

                _log("Using [{0}] attempt {1}".format(slot.display_name, attempt + 1))
                sublime.set_timeout(
                    lambda n=slot.display_name: _set_status(
                        view, "CC: [{0}] …".format(n)
                    ), 0
                )

                # ── Build request ─────────────────────────────────────────────
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

                # ── Make the call ─────────────────────────────────────────────
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
                    _log("Raw response [{0}] ({1} chars): {2!r}".format(
                        slot.display_name, len(raw), raw[:200]
                    ))

                    if cancel.is_set():
                        return
                    if pending_requests.get(vid, (None,))[0] != req_id:
                        return

                    completion = clean_completion(raw)
                    _log("Cleaned [{0}]: {1!r}".format(
                        slot.display_name, completion[:120]
                    ))

                    if completion:
                        sublime.set_timeout(
                            lambda c=completion: _present(view, cursor, c), 0
                        )
                    else:
                        sublime.set_timeout(
                            lambda: _set_status(view, "CC: no suggestion"), 0
                        )
                    return  # ← done

                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        _log("429 on [{0}], rotating key …".format(slot.display_name))
                        pool.report_429(slot)
                        # Loop → try next slot
                    else:
                        _log_error("HTTP {0} on [{1}]".format(e.code, slot.display_name))
                        pool.report_error(slot)
                        sublime.set_timeout(
                            lambda c=e.code: _set_status(
                                view, "CC: HTTP {0} error".format(c)
                            ), 0
                        )
                        return

                except urllib.error.URLError as e:
                    _log_error("Network error on [{0}]: {1}".format(
                        slot.display_name, str(e)[:80]
                    ))
                    pool.report_error(slot)
                    sublime.set_timeout(
                        lambda: _set_status(view, "CC: network error"), 0
                    )
                    return

                except Exception as e:
                    _log_error("Error on [{0}]: {1}".format(
                        slot.display_name, str(e)[:80]
                    ))
                    pool.report_error(slot)
                    sublime.set_timeout(
                        lambda: _set_status(view, "CC: error — see console"), 0
                    )
                    return

            # Exhausted all attempts
            sublime.set_timeout(
                lambda: _set_status(view, "CC: all models busy, try again soon"), 0
            )

        threading.Thread(target=fetch, daemon=True).start()


def _present(view, cursor, text):
    _clear_status(view)
    show_phantom(view, cursor, text)


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
                ps.update([sublime.Phantom(
                    sublime.Region(new_pos, new_pos),
                    '<span style="color:gray;font-style:italic;">{0}</span>'.format(
                        html.escape("\n".join(rem))
                    ),
                    sublime.LAYOUT_INLINE,
                )])
                phantoms[vid] = (ps, rem, "")
            else:
                clear_phantoms(view)
        finally:
            accept_grace[vid] = time.time() + 0.25
            suppress_clear.discard(vid)