"""suggest.py — CodeContinue inline suggestion engine.

Key design points:
  • Symbol context goes in the Ollama `system` field (not injected into FIM prefix)
    → model never confuses metadata comments with code to complete
  • Current file symbols extracted SYNCHRONOUSLY from view content (always fresh)
  • Neighbor files: same directory only, lightweight cache, toggleable via setting
  • FIM prefix/suffix contain only real code — clean signal for the model
  • Request cancellation via threading.Event
"""

import html
import json
import os
import subprocess
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
from .text_utils import clean_markdown_fences, strip_common_indent


# ──────────────────────────────────────────────────────────────────────────────
# Language registry
# ──────────────────────────────────────────────────────────────────────────────

_LANG = {
    "rs": {
        "name": "Rust", "fence": "rust",
        "stops": ["\n\n", "\nfn ", "\npub fn ", "\nimpl ", "\nstruct ", "\nenum ", "\nmod "],
        "system": (
            "You are a Rust code completion engine. "
            "Your only job: output the next 1-3 lines of code that naturally follow "
            "the given context. Rules:\n"
            "- Output ONLY the code, nothing else.\n"
            "- No explanations, no markdown, no backticks.\n"
            "- Match existing indentation and style exactly.\n"
            "- Use types, functions, and structs already present in the context.\n"
            "- Do NOT invent new abstractions. Complete what is already started.\n"
            "- Stop before starting a new fn/impl/struct/mod block."
        ),
    },
    "py": {
        "name": "Python", "fence": "python",
        "stops": ["\n\n", "\ndef ", "\nclass ", "\n# ---"],
        "system": (
            "You are a Python code completion engine. "
            "Output the next 1-3 lines that naturally follow the context. "
            "Output ONLY the code — no explanations, no markdown."
        ),
    },
    "js": {
        "name": "JavaScript", "fence": "javascript",
        "stops": ["\n\n", "\nfunction ", "\nconst ", "\nlet ", "\nvar ", "\nclass "],
        "system": (
            "You are a JavaScript code completion engine. "
            "Output the next 1-3 lines that naturally follow the context. "
            "Use modern ES2020+ syntax. Output ONLY the code."
        ),
    },
    "ts": {
        "name": "TypeScript", "fence": "typescript",
        "stops": ["\n\n", "\nfunction ", "\nconst ", "\nlet ", "\ninterface ", "\ntype "],
        "system": (
            "You are a TypeScript code completion engine. "
            "Output the next 1-3 lines that naturally follow the context. "
            "Use proper types. Output ONLY the code."
        ),
    },
    "go": {
        "name": "Go", "fence": "go",
        "stops": ["\n\n", "\nfunc ", "\ntype ", "\nvar "],
        "system": (
            "You are a Go code completion engine. "
            "Output the next 1-3 lines that naturally follow the context. "
            "Follow Go idioms. Output ONLY the code."
        ),
    },
    "java":  {"name": "Java",       "fence": "java",       "stops": ["\n\n", "\npublic ", "\nclass "],         "system": "You are a Java code completion engine. Output ONLY the next 1-3 lines of code."},
    "kt":    {"name": "Kotlin",     "fence": "kotlin",     "stops": ["\n\n", "\nfun ", "\nclass "],            "system": "You are a Kotlin code completion engine. Output ONLY the next 1-3 lines of code."},
    "cpp":   {"name": "C++",        "fence": "cpp",        "stops": ["\n\n", "\nvoid ", "\nint ", "\nclass "], "system": "You are a C++ code completion engine. Output ONLY the next 1-3 lines of code."},
    "cc":    {"name": "C++",        "fence": "cpp",        "stops": ["\n\n", "\nvoid ", "\nint "],             "system": "You are a C++ code completion engine. Output ONLY the next 1-3 lines of code."},
    "hpp":   {"name": "C++ Header", "fence": "cpp",        "stops": ["\n\n"],                                  "system": "You are a C++ code completion engine. Output ONLY the next 1-3 lines of code."},
    "c":     {"name": "C",          "fence": "c",          "stops": ["\n\n", "\nvoid ", "\nint "],             "system": "You are a C code completion engine. Output ONLY the next 1-3 lines of code."},
    "h":     {"name": "C Header",   "fence": "c",          "stops": ["\n\n"],                                  "system": "You are a C code completion engine. Output ONLY the next 1-3 lines of code."},
    "rb":    {"name": "Ruby",       "fence": "ruby",       "stops": ["\n\n", "\ndef ", "\nclass "],            "system": "You are a Ruby code completion engine. Output ONLY the next 1-3 lines of code."},
    "php":   {"name": "PHP",        "fence": "php",        "stops": ["\n\n", "\nfunction "],                   "system": "You are a PHP code completion engine. Output ONLY the next 1-3 lines of code."},
    "sql":   {"name": "SQL",        "fence": "sql",        "stops": ["\n\n"],                                  "system": "You are a SQL completion engine. Output ONLY the next 1-3 lines of SQL."},
    "toml":  {"name": "TOML",       "fence": "toml",       "stops": ["\n\n", "\n["],                           "system": "You are a TOML completion engine. Output ONLY the continuation."},
    "yaml":  {"name": "YAML",       "fence": "yaml",       "stops": ["\n\n"],                                  "system": "You are a YAML completion engine. Match indentation exactly. Output ONLY the continuation."},
    "yml":   {"name": "YAML",       "fence": "yaml",       "stops": ["\n\n"],                                  "system": "You are a YAML completion engine. Output ONLY the continuation."},
    "json":  {"name": "JSON",       "fence": "json",       "stops": ["\n\n", "\n}"],                           "system": "Complete the JSON. Output ONLY valid JSON continuation."},
    "html":  {"name": "HTML",       "fence": "html",       "stops": ["\n\n"],                                  "system": "You are an HTML completion engine. Output ONLY the next 1-3 lines."},
    "css":   {"name": "CSS",        "fence": "css",        "stops": ["\n\n", "\n}"],                           "system": "You are a CSS completion engine. Output ONLY the next 1-3 lines."},
    "scss":  {"name": "SCSS",       "fence": "scss",       "stops": ["\n\n", "\n}"],                           "system": "You are a SCSS completion engine. Output ONLY the next 1-3 lines."},
    "sh":    {"name": "Bash",       "fence": "bash",       "stops": ["\n\n", "\nfunction "],                   "system": "You are a Bash completion engine. Output ONLY the next 1-3 lines."},
    "bash":  {"name": "Bash",       "fence": "bash",       "stops": ["\n\n"],                                  "system": "You are a Bash completion engine. Output ONLY the next 1-3 lines."},
    "md":    {"name": "Markdown",   "fence": "markdown",   "stops": ["\n\n\n"],                                "system": "Continue the Markdown document. Output ONLY the continuation."},
    "_default": {
        "name": "code", "fence": "", "stops": ["\n\n"],
        "system": "You are a code completion engine. Output ONLY the next 1-3 lines of code.",
    },
}

_SYNTAX_MAP = {
    "rust": "rs", "python": "py", "javascript": "js", "typescript": "ts",
    "go": "go", "java": "java", "kotlin": "kt", "c++": "cpp", "c": "c",
    "ruby": "rb", "php": "php", "sql": "sql", "toml": "toml",
    "yaml": "yaml", "json": "json", "html": "html", "css": "css",
    "scss": "scss", "bash": "sh", "shell": "sh", "shellscript": "sh",
    "markdown": "md",
}


def detect_language(view):
    fp = view.file_name() or ""
    if fp:
        ext = os.path.splitext(fp)[1].lstrip(".").lower()
        if ext in _LANG:
            return _LANG[ext], ext
    syntax = view.syntax()
    if syntax:
        ext = _SYNTAX_MAP.get(syntax.name.lower())
        if ext:
            return _LANG[ext], ext
    return _LANG["_default"], "_default"


# ──────────────────────────────────────────────────────────────────────────────
# API helpers
# ──────────────────────────────────────────────────────────────────────────────

_FIM_PRE = "<|fim_prefix|>"
_FIM_SUF = "<|fim_suffix|>"
_FIM_MID = "<|fim_middle|>"


def _is_ollama(endpoint):
    return "/api/generate" in endpoint


def _fim_prompt(before, after):
    return _FIM_PRE + before + _FIM_SUF + after + _FIM_MID


def _chat_messages(before, after, lang, symbol_ctx):
    system = lang["system"]
    if symbol_ctx:
        system += "\n\nProject symbols (use these — do not invent new ones):\n" + symbol_ctx

    fence = lang["fence"]
    name  = lang["name"]
    if after.strip():
        user = (
            "{n} — complete the gap at [CURSOR]:\n```{f}\n{b}[CURSOR]{a}\n```\n"
            "Output ONLY what replaces [CURSOR]."
        ).format(n=name, f=fence, b=before, a=after[:300])
    else:
        user = (
            "Continue this {n} code:\n```{f}\n{b}\n```\nOutput ONLY the continuation."
        ).format(n=name, f=fence, b=before)

    return [{"role": "system", "content": system},
            {"role": "user",   "content": user}]


# ──────────────────────────────────────────────────────────────────────────────
# Project tree (kept from original fork, used only in chat mode fallback)
# ──────────────────────────────────────────────────────────────────────────────

def _project_tree(file_path):
    if not file_path:
        return ""
    parent = os.path.dirname(file_path)
    try:
        r = subprocess.run(
            ["tree", "-L", "1", "--dirsfirst",
             "-I", "target|__pycache__|node_modules|.git"],
            cwd=parent, capture_output=True, text=True, timeout=1.0,
        )
        if r.returncode == 0:
            return r.stdout[:300]
    except Exception:
        pass
    try:
        files = sorted(f for f in os.listdir(parent)
                       if not f.startswith(".") and f != "target")
        return "\n".join(files[:20])
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Plugin state
# ──────────────────────────────────────────────────────────────────────────────

phantoms         = {}   # view.id() -> (PhantomSet, [lines], common_prefix)
last_request     = {}   # view.id() -> float
pending_requests = {}   # view.id() -> (request_id, threading.Event)
suppress_clear   = set()
accept_grace     = {}   # view.id() -> float


# ──────────────────────────────────────────────────────────────────────────────
# Phantom display
# ──────────────────────────────────────────────────────────────────────────────

def show_phantom(view, cursor, suggestion):
    clear_phantoms(view)
    ps = sublime.PhantomSet(view)
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
# Listener
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
        endpoint = settings.get("endpoint", "")
        model    = settings.get("model", "")

        if not is_endpoint_configured(settings):
            sublime.status_message("CodeContinue: Endpoint not configured.")
            sublime.set_timeout(lambda: show_endpoint_config_panel(view), 100)
            return

        sel = view.sel()
        if len(sel) != 1:
            return

        cursor = sel[0].begin()
        lang, ext = detect_language(view)
        _log("Language: {0} (ext={1})".format(lang["name"], ext))

        # ── Context window ────────────────────────────────────────────────────
        max_lines    = settings.get("max_context_lines", 60)
        n_before     = (max_lines * 2) // 3
        n_after      = max_lines // 3
        total_rows   = view.rowcol(view.size())[0] + 1
        cur_row, _   = view.rowcol(cursor)
        start_row    = max(0, cur_row - n_before)
        end_row      = min(total_rows, cur_row + n_after + 1)
        start_pt     = view.text_point(start_row, 0)
        end_pt       = view.text_point(end_row, 0) if end_row < total_rows else view.size()

        full         = view.substr(sublime.Region(start_pt, end_pt))
        off          = cursor - start_pt
        code_before  = full[:off]
        code_after   = full[off:]

        # ── Symbol context (Rust only, synchronous for current file) ──────────
        symbol_ctx = ""
        if ext == "rs":
            view_content    = view.substr(sublime.Region(0, view.size()))
            index_neighbors = settings.get("index_neighboring_files", True)
            symbol_ctx      = get_symbol_context(
                view.file_name(), view_content,
                index_neighbors=index_neighbors, max_chars=400,
            )
            _log("Symbol context ({0} chars):\n{1}".format(
                len(symbol_ctx), symbol_ctx
            ))

        # ── Cancel previous request ───────────────────────────────────────────
        vid = view.id()
        old = pending_requests.get(vid)
        if old:
            old[1].set()

        cancel      = threading.Event()
        req_id      = (vid, cursor, time.time())
        pending_requests[vid] = (req_id, cancel)

        sublime.status_message("CodeContinue: Fetching …")

        use_fim     = settings.get("use_fim", _is_ollama(endpoint))
        temperature = settings.get("temperature", 0.2)
        top_p       = settings.get("top_p", 0.85)
        max_tokens  = settings.get("max_tokens", 150)
        timeout_s   = settings.get("timeout_ms", 30000) / 1000.0
        stops       = lang["stops"]
        headers     = build_api_headers(settings)

        def fetch():
            try:
                if cancel.is_set():
                    return
                completion = None

                if use_fim and _is_ollama(endpoint):
                    # ── Ollama FIM ────────────────────────────────────────────
                    # Symbol context goes in `system` field — NOT in the prefix.
                    # FIM prefix/suffix contain only real code.
                    system_msg = lang["system"]
                    if symbol_ctx:
                        system_msg += (
                            "\n\nProject symbols (reference only — complete the code, "
                            "do not repeat these):\n" + symbol_ctx
                        )
                    payload = {
                        "model":  model,
                        "system": system_msg,
                        "prompt": _fim_prompt(code_before, code_after),
                        "stream": False,
                        "options": {
                            "temperature": temperature,
                            "top_p":       top_p,
                            "num_predict": max_tokens,
                            "stop":        stops,
                        },
                    }
                    req = urllib.request.Request(
                        endpoint, data=json.dumps(payload).encode(), headers=headers
                    )
                    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                        completion = json.loads(resp.read()).get("response", "").strip()

                else:
                    # ── OpenAI-compatible chat ────────────────────────────────
                    msgs = _chat_messages(code_before, code_after, lang, symbol_ctx)
                    payload = {
                        "model":       model,
                        "messages":    msgs,
                        "max_tokens":  max_tokens,
                        "temperature": temperature,
                        "top_p":       top_p,
                    }
                    if stops:
                        payload["stop"] = stops[:4]
                    req = urllib.request.Request(
                        endpoint, data=json.dumps(payload).encode(), headers=headers
                    )
                    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                        body    = json.loads(resp.read())
                        choices = body.get("choices", [])
                        if choices:
                            completion = (
                                choices[0].get("message", {}).get("content", "")
                                or choices[0].get("text", "")
                            ).strip()

                if cancel.is_set():
                    return
                if pending_requests.get(vid, (None,))[0] != req_id:
                    return

                if completion:
                    completion = clean_markdown_fences(completion)
                    _log("Suggestion ({0}): {1!r}".format(lang["name"], completion[:80]))
                    sublime.set_timeout(lambda: _show(view, cursor, completion), 0)
                else:
                    sublime.set_timeout(
                        lambda: sublime.status_message("CodeContinue: No suggestion."), 0
                    )

            except urllib.error.URLError as e:
                _log_error("Network: {0}".format(str(e)[:100]))
                sublime.set_timeout(
                    lambda: sublime.status_message("CodeContinue: Network error."), 0
                )
            except Exception as e:
                _log_error("Error: {0}".format(str(e)[:100]))
                sublime.set_timeout(
                    lambda: sublime.status_message("CodeContinue: Error — see console."), 0
                )

        threading.Thread(target=fetch, daemon=True).start()


def _show(view, cursor, text):
    sublime.status_message("")
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
            pos        = view.sel()[0].begin()
            first      = remaining.pop(0)
            rem        = [common_prefix + l for l in remaining] if common_prefix else remaining
            insert_txt = first + ("\n" if rem else "")
            view.insert(edit, pos, insert_txt)

            new_pos = pos + len(insert_txt)
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