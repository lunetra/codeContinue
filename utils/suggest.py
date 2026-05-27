"""suggest.py — CodeContinue inline code completion engine.

Design goals:
  - Reliable with small models (qwen2.5-coder:3b and up)
  - Fast, pattern-focused completions — not creative writing
  - JetBrains-style: knows the project, suggests realistic continuations

Key decisions:
  - Chat-completions API only (works everywhere — Ollama, OpenAI, OpenRouter…)
  - NO stop sequences: they cause empty responses when models wrap output in
    markdown fences. max_tokens limits length naturally.
  - Robust post-cleanup handles ```fences, special tokens, and trailing
    explanation paragraphs.
  - Same-directory file listing always included for the language → key context
    for module files (mod.rs, __init__.py, index.ts, etc.)
  - Rust symbol index optional via setting (off by default).
  - Request cancellation: new request kills the in-flight one.
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

from .api import build_api_headers
from .log import _log, _log_error
from .settings import is_endpoint_configured, show_endpoint_config_panel
from .symbol_index import get_symbol_context, on_file_saved
from .text_utils import strip_common_indent


# ──────────────────────────────────────────────────────────────────────────────
# Language registry
# ──────────────────────────────────────────────────────────────────────────────
# Minimal — just the human name and fence identifier. The same simple prompt
# template is used for every language; language-specific notes are kept short
# so they don't overwhelm small models.

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
    """Return (lang_config, ext_key). Priority: extension > syntax > default."""
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
# Directory listing (sibling files, same extension)
# ──────────────────────────────────────────────────────────────────────────────

def list_sibling_modules(file_path, ext):
    """Return comma-separated stems of sibling files with the same extension.

    Crucial context for module files (mod.rs, __init__.py, index.ts) — tells
    the model which `mod X;` / `use X;` / `import X` lines are likely.
    """
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
# Output cleaning — robust to markdown fences and special tokens
# ──────────────────────────────────────────────────────────────────────────────

_SPECIAL_TOKEN_RE = re.compile(
    r"<\|(?:im_start|im_end|endoftext|begin_of_text|end_of_text|"
    r"eot_id|start_header_id|end_header_id|fim_prefix|fim_middle|"
    r"fim_suffix|file_separator|user|assistant|system)\|>"
    r"|\[(?:END_OF_TEXT|/?INST)\]"
)


def clean_completion(text):
    """
    Robustly extract code from a chat-model response.

    Handles:
      - Leading blank lines / whitespace
      - Markdown fences (```rust, ```python, ```)
      - Trailing explanation paragraphs after a closing fence
      - Special LLM tokens
      - Trailing blank lines

    Preserves indentation on real code lines.
    """
    if not text:
        return ""

    # If response starts with a markdown fence (possibly after blank lines),
    # strip it (incl. language tag). \s* handles any leading whitespace before
    # the fence, but we DON'T lstrip the whole string — that would kill
    # indentation on real code responses.
    text = re.sub(r"^\s*```[a-zA-Z_+\-]*\s*\n?", "", text)

    # If there's a closing ``` anywhere, everything after is explanation — drop it
    if "```" in text:
        text = text.split("```", 1)[0]

    # Strip known special tokens
    text = _SPECIAL_TOKEN_RE.sub("", text)

    # Split into lines, drop leading blanks (preserving indentation on code),
    # drop trailing blanks
    lines = text.split("\n")
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Prompt building
# ──────────────────────────────────────────────────────────────────────────────

def build_messages(code_before, code_after, lang, ext, extra_context):
    """Build OpenAI-style chat messages.

    Simple, direct prompt that works well with small coding models.
    """
    name  = lang["name"]
    fence = lang["fence"]

    system = (
        "You are a {name} code autocomplete engine. Your output is inserted "
        "directly into the user's file at the cursor position.\n\n"
        "Rules — follow them exactly:\n"
        "1. Output ONLY raw code. No markdown fences, no backticks, no explanations.\n"
        "2. Match the existing indentation and style of the surrounding code.\n"
        "3. Output 1-4 lines that naturally continue the pattern.\n"
        "4. Use types, functions, modules, and names already visible in the context.\n"
        "5. Do NOT invent new abstractions. Do NOT repeat code that already exists.\n"
        "6. Stop when the immediate logical unit is complete."
    ).format(name=name)

    if extra_context:
        system += "\n\nProject context (for reference only — do not repeat):\n" + extra_context

    # User message: show the code with a fence so the model knows where it ends.
    # The cursor position is implicit — code_before ends, code_after follows.
    if code_after.strip():
        # Mid-file completion — show both sides
        user = (
            "Complete the gap marked <CURSOR/> in this {name} code. "
            "Output ONLY the text that goes at <CURSOR/>:\n\n"
            "```{fence}\n{before}<CURSOR/>{after}\n```"
        ).format(name=name, fence=fence,
                 before=code_before,
                 after=code_after[:400])
    else:
        # End-of-file completion — just show the prefix
        user = (
            "Continue this {name} code from where it ends. "
            "Output ONLY the continuation (no repeating existing code):\n\n"
            "```{fence}\n{before}\n```"
        ).format(name=name, fence=fence, before=code_before)

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Plugin state
# ──────────────────────────────────────────────────────────────────────────────

phantoms         = {}   # view.id() -> (PhantomSet, [lines], common_prefix)
last_request     = {}   # view.id() -> float (debounce)
pending_requests = {}   # view.id() -> (request_id, cancel_event)
suppress_clear   = set()
accept_grace     = {}   # view.id() -> float


# ──────────────────────────────────────────────────────────────────────────────
# Phantom (inline ghost text) display
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

        # Empty trigger_language list = enabled for all
        if trigger_langs and not (
            ext in [t.lower() for t in trigger_langs]
            or any(t.lower() in syntax_name for t in trigger_langs)
        ):
            return None

        vid = view.id()
        if vid in phantoms:
            return None

        # Debounce — at most one request per second
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
        endpoint = settings.get("endpoint", "")
        model    = settings.get("model", "")

        if not is_endpoint_configured(settings):
            sublime.status_message("CodeContinue: Endpoint not configured.")
            sublime.set_timeout(lambda: show_endpoint_config_panel(view), 100)
            return

        sel = view.sel()
        if len(sel) != 1:
            return

        cursor    = sel[0].begin()
        lang, ext = detect_language(view)
        _log("Language: {0} (ext={1})".format(lang["name"], ext))

        # ── Slice context window (before + after cursor) ──────────────────────
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

        # ── Build extra context (directory listing + optional symbols) ────────
        extra_parts = []
        file_path   = view.file_name()

        # 1. Sibling modules — always useful, especially for module files
        siblings = list_sibling_modules(file_path, ext)
        if siblings:
            extra_parts.append(
                "Other {0} files in same directory: {1}".format(ext, siblings)
            )

        # 2. Rust symbol index — opt-in via setting
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

        # ── Cancel previous in-flight request ─────────────────────────────────
        vid = view.id()
        old = pending_requests.get(vid)
        if old:
            old[1].set()

        cancel = threading.Event()
        req_id = (vid, cursor, time.time())
        pending_requests[vid] = (req_id, cancel)

        sublime.status_message("CodeContinue: Fetching …")

        # ── Parameters ────────────────────────────────────────────────────────
        temperature = settings.get("temperature", 0.2)
        top_p       = settings.get("top_p", 0.9)
        max_tokens  = settings.get("max_tokens", 200)
        timeout_s   = settings.get("timeout_ms", 30000) / 1000.0
        headers     = build_api_headers(settings)

        messages = build_messages(code_before, code_after, lang, ext, extra_context)

        # NO stop sequences — they cause empty responses when models wrap output
        # in markdown fences. max_tokens caps the length, post-cleanup strips
        # whatever wrapping the model adds.
        payload = {
            "model":       model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "top_p":       top_p,
        }

        def fetch():
            try:
                if cancel.is_set():
                    return

                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload).encode(),
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    body    = json.loads(resp.read())
                    choices = body.get("choices", [])
                    raw     = ""
                    if choices:
                        raw = (
                            choices[0].get("message", {}).get("content", "")
                            or choices[0].get("text", "")
                        )

                if cancel.is_set():
                    return
                if pending_requests.get(vid, (None,))[0] != req_id:
                    return

                _log("Raw response ({0} chars): {1!r}".format(len(raw), raw[:200]))

                completion = clean_completion(raw)

                _log("Cleaned: {0!r}".format(completion[:200]))

                if completion:
                    sublime.set_timeout(lambda: _present(view, cursor, completion), 0)
                else:
                    sublime.set_timeout(
                        lambda: sublime.status_message("CodeContinue: No suggestion."), 0
                    )

            except urllib.error.URLError as e:
                _log_error("Network: {0}".format(str(e)[:120]))
                sublime.set_timeout(
                    lambda: sublime.status_message("CodeContinue: Network error."), 0
                )
            except Exception as e:
                _log_error("Error: {0}".format(str(e)[:120]))
                sublime.set_timeout(
                    lambda: sublime.status_message("CodeContinue: Error — see console."), 0
                )

        threading.Thread(target=fetch, daemon=True).start()


def _present(view, cursor, text):
    sublime.status_message("")
    show_phantom(view, cursor, text)


# ──────────────────────────────────────────────────────────────────────────────
# Accept command  (Tab — accept one line at a time)
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