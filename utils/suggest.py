"""suggest.py — CodeContinue inline suggestion engine (improved fork).

Changes from original:
  • Dynamic language detection by file extension (.rs, .py, .js, .sql, .toml …)
  • Language-specific system prompts and stop sequences
  • FIM (Fill-in-the-Middle) support for Ollama /api/generate endpoints
  • Suffix context sent in chat mode too — model knows what comes after cursor
  • Request cancellation: new request kills the previous in-flight one instantly
  • Configurable temperature / top_p / max_tokens via settings
  • project_context (tree command) kept from original fork
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
from .text_utils import clean_markdown_fences, strip_common_indent


# ──────────────────────────────────────────────────────────────────────────────
# Language registry
# ──────────────────────────────────────────────────────────────────────────────

# Maps lowercase file extension (without dot) -> language config dict.
# Keys:
#   name   - human-readable name used in prompts
#   fence  - markdown code-fence identifier
#   stops  - stop sequences (keep completions short and clean)
#   system - system prompt tuned for fast pattern completion in this language

_LANG_REGISTRY = {
    "rs": {
        "name": "Rust",
        "fence": "rust",
        "stops": ["\n\n", "\nfn ", "\npub fn ", "\nimpl ", "\nstruct ", "\nenum ", "\nmod "],
        "system": (
            "You are an expert Rust developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Follow Rust idioms: correct ownership/borrowing, prefer iterators, "
            "handle errors with Result/Option, use the ? operator where appropriate. "
            "Output ONLY the code continuation — no explanations, no markdown fences, "
            "no trailing blank lines."
        ),
    },
    "py": {
        "name": "Python",
        "fence": "python",
        "stops": ["\n\n", "\ndef ", "\nclass ", "\n# ---", "\nif __name__"],
        "system": (
            "You are an expert Python developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Write idiomatic Pythonic code — prefer comprehensions and context managers. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "js": {
        "name": "JavaScript",
        "fence": "javascript",
        "stops": ["\n\n", "\nfunction ", "\nconst ", "\nlet ", "\nvar ", "\nclass ", "\nexport "],
        "system": (
            "You are an expert JavaScript developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Use modern ES2020+ syntax, async/await, destructuring. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "ts": {
        "name": "TypeScript",
        "fence": "typescript",
        "stops": ["\n\n", "\nfunction ", "\nconst ", "\nlet ", "\ninterface ", "\ntype ", "\nclass "],
        "system": (
            "You are an expert TypeScript developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Use proper TypeScript types and generics where appropriate. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "go": {
        "name": "Go",
        "fence": "go",
        "stops": ["\n\n", "\nfunc ", "\ntype ", "\nvar ", "\nconst "],
        "system": (
            "You are an expert Go developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Follow Go idioms: explicit error handling, simple interfaces, clear naming. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "java": {
        "name": "Java",
        "fence": "java",
        "stops": ["\n\n", "\npublic ", "\nprivate ", "\nprotected ", "\nclass "],
        "system": (
            "You are an expert Java developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "kt": {
        "name": "Kotlin",
        "fence": "kotlin",
        "stops": ["\n\n", "\nfun ", "\nclass ", "\nobject ", "\nval ", "\nvar "],
        "system": (
            "You are an expert Kotlin developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Use Kotlin idioms: extension functions, data classes, coroutines. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "cpp": {
        "name": "C++",
        "fence": "cpp",
        "stops": ["\n\n", "\nvoid ", "\nint ", "\nbool ", "\nclass ", "\nstruct "],
        "system": (
            "You are an expert C++ developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Use modern C++17/20 features where appropriate. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "cc": {
        "name": "C++", "fence": "cpp",
        "stops": ["\n\n", "\nvoid ", "\nint ", "\nbool "],
        "system": (
            "You are an expert C++ developer. Complete the code exactly where it is left off. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "hpp": {
        "name": "C++ Header", "fence": "cpp",
        "stops": ["\n\n", "\nvoid ", "\nint "],
        "system": (
            "You are an expert C++ developer. Complete the header file exactly where it is left off. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "c": {
        "name": "C",
        "fence": "c",
        "stops": ["\n\n", "\nvoid ", "\nint ", "\nbool ", "\nstruct "],
        "system": (
            "You are an expert C developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "h": {
        "name": "C/C++ Header", "fence": "c",
        "stops": ["\n\n", "\nvoid ", "\nint "],
        "system": (
            "You are an expert C/C++ developer. Complete the header file exactly where it is left off. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "rb": {
        "name": "Ruby", "fence": "ruby",
        "stops": ["\n\n", "\ndef ", "\nclass ", "\nmodule "],
        "system": (
            "You are an expert Ruby developer. Complete the code exactly where it is left off. "
            "Write idiomatic Ruby. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "php": {
        "name": "PHP", "fence": "php",
        "stops": ["\n\n", "\nfunction ", "\nclass ", "\npublic ", "\nprivate "],
        "system": (
            "You are an expert PHP developer. Complete the code exactly where it is left off. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "sql": {
        "name": "SQL", "fence": "sql",
        "stops": ["\n\n", "\nSELECT ", "\nINSERT ", "\nUPDATE ", "\nDELETE ", "\nCREATE "],
        "system": (
            "You are an expert SQL developer. Complete the SQL exactly where it is left off. "
            "Write clean, readable SQL. "
            "Output ONLY the SQL continuation — no explanations, no markdown fences."
        ),
    },
    "toml": {
        "name": "TOML", "fence": "toml",
        "stops": ["\n\n", "\n["],
        "system": (
            "You are an expert at TOML configuration files (especially Cargo.toml). "
            "Complete the TOML exactly where it is left off. "
            "Output ONLY the continuation — no explanations, no markdown fences."
        ),
    },
    "yaml": {
        "name": "YAML", "fence": "yaml",
        "stops": ["\n\n"],
        "system": (
            "You are an expert at YAML configuration. "
            "Complete the YAML exactly where it is left off, matching indentation precisely. "
            "Output ONLY the continuation — no explanations, no markdown fences."
        ),
    },
    "yml": {
        "name": "YAML", "fence": "yaml",
        "stops": ["\n\n"],
        "system": (
            "You are an expert at YAML configuration. "
            "Complete the YAML exactly where it is left off. "
            "Output ONLY the continuation — no explanations, no markdown fences."
        ),
    },
    "json": {
        "name": "JSON", "fence": "json",
        "stops": ["\n\n", "\n}"],
        "system": (
            "You are completing a JSON file. "
            "Continue exactly where it is left off, maintaining valid JSON. "
            "Output ONLY the continuation — no explanations."
        ),
    },
    "html": {
        "name": "HTML", "fence": "html",
        "stops": ["\n\n"],
        "system": (
            "You are an expert HTML developer. Complete the HTML exactly where it is left off. "
            "Output ONLY the code continuation — no explanations."
        ),
    },
    "css": {
        "name": "CSS", "fence": "css",
        "stops": ["\n\n", "\n}"],
        "system": (
            "You are an expert CSS developer. Complete the CSS exactly where it is left off. "
            "Output ONLY the code continuation — no explanations."
        ),
    },
    "scss": {
        "name": "SCSS", "fence": "scss",
        "stops": ["\n\n", "\n}"],
        "system": (
            "You are an expert SCSS developer. Complete the SCSS exactly where it is left off. "
            "Output ONLY the code continuation — no explanations."
        ),
    },
    "sh": {
        "name": "Bash", "fence": "bash",
        "stops": ["\n\n", "\nfunction ", "\n# ---"],
        "system": (
            "You are an expert Bash script developer. Complete the script exactly where it is left off. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
    "bash": {
        "name": "Bash", "fence": "bash",
        "stops": ["\n\n", "\nfunction "],
        "system": (
            "You are an expert Bash script developer. Complete the script exactly where it is left off. "
            "Output ONLY the code continuation — no explanations."
        ),
    },
    "md": {
        "name": "Markdown", "fence": "markdown",
        "stops": ["\n\n\n"],
        "system": (
            "You are completing a Markdown document. "
            "Continue exactly where it is left off, matching the writing style. "
            "Output ONLY the continuation."
        ),
    },
    "_default": {
        "name": "code", "fence": "",
        "stops": ["\n\n"],
        "system": (
            "You are an expert developer focused on fast inline code completion. "
            "Complete the code exactly where it is left off. "
            "Output ONLY the code continuation — no explanations, no markdown fences."
        ),
    },
}

# Sublime syntax name -> extension key (fallback when file has no extension)
_SYNTAX_TO_EXT = {
    "rust": "rs", "python": "py", "javascript": "js", "typescript": "ts",
    "go": "go", "java": "java", "kotlin": "kt", "c++": "cpp", "c": "c",
    "ruby": "rb", "php": "php", "sql": "sql", "toml": "toml",
    "yaml": "yaml", "json": "json", "html": "html", "css": "css",
    "scss": "scss", "bash": "sh", "shell": "sh", "shellscript": "sh",
    "markdown": "md",
}


def detect_language(view):
    """Return (lang_config_dict, ext_key) for the given view.

    Priority: file extension > Sublime syntax name > _default fallback.
    """
    file_path = view.file_name() or ""
    if file_path:
        _, ext = os.path.splitext(file_path)
        ext_key = ext.lstrip(".").lower()
        if ext_key in _LANG_REGISTRY:
            return _LANG_REGISTRY[ext_key], ext_key

    syntax = view.syntax()
    if syntax:
        syntax_name = syntax.name.lower()
        ext_key = _SYNTAX_TO_EXT.get(syntax_name)
        if ext_key:
            return _LANG_REGISTRY[ext_key], ext_key

    return _LANG_REGISTRY["_default"], "_default"


# ──────────────────────────────────────────────────────────────────────────────
# FIM helpers  (Qwen2.5-Coder, DeepSeek-Coder, StarCoder2 …)
# ──────────────────────────────────────────────────────────────────────────────

_FIM_PREFIX = "<|fim_prefix|>"
_FIM_SUFFIX = "<|fim_suffix|>"
_FIM_MIDDLE = "<|fim_middle|>"


def _is_ollama_generate(endpoint):
    return "/api/generate" in endpoint


def _build_fim_prompt(code_before, code_after):
    return _FIM_PREFIX + code_before + _FIM_SUFFIX + code_after + _FIM_MIDDLE


def _build_chat_messages(code_before, code_after, lang_config):
    """OpenAI-style messages array.
    Includes suffix so the model knows what comes after the cursor (gap filling).
    """
    system = lang_config["system"]
    fence  = lang_config["fence"]
    name   = lang_config["name"]

    if code_after.strip():
        user_content = (
            "{name} code — complete the gap at [CURSOR].\n\n"
            "```{fence}\n{before}[CURSOR]{after}\n```\n\n"
            "Output ONLY the text that replaces [CURSOR]. "
            "Do not repeat any code before or after the marker."
        ).format(name=name, fence=fence, before=code_before, after=code_after[:400])
    else:
        user_content = (
            "Continue this {name} code from where it ends:\n\n"
            "```{fence}\n{before}\n```\n\n"
            "Output ONLY the continuation. Do not repeat existing code."
        ).format(name=name, fence=fence, before=code_before)

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_content},
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Project context  (kept from the original fork — uses the `tree` command)
# ──────────────────────────────────────────────────────────────────────────────

def get_project_context(file_path, max_depth=2):
    if not file_path:
        return ""
    file_name  = os.path.basename(file_path)
    parent_dir = os.path.dirname(file_path)
    try:
        rel_path = os.path.relpath(file_path, parent_dir)
    except ValueError:
        rel_path = file_name

    context = "File: {0}\nPath: {1}\n\n".format(file_name, rel_path)
    try:
        result = subprocess.run(
            ["tree", "-L", str(max_depth), "-a", "--dirsfirst",
             "-I", "target|__pycache__|node_modules|*.lock|*.log|.git"],
            cwd=parent_dir,
            capture_output=True, text=True, timeout=1.2,
        )
        if result.returncode == 0:
            return context + "Project Structure:\n" + result.stdout
    except Exception:
        pass

    # Fallback: manual os.walk
    context += "Project Structure:\n"
    try:
        for root, dirs, files in os.walk(parent_dir):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".")
                       and d not in ("target", "__pycache__", "node_modules")]
            level = root.replace(parent_dir, "").count(os.sep)
            if level > max_depth:
                continue
            indent = "    " * level
            context += "{0}|-- {1}/\n".format(indent, os.path.basename(root))
            if level < max_depth:
                for f in sorted(files)[:15]:
                    context += "{0}    |-- {1}\n".format(indent, f)
    except Exception:
        context += "  (could not generate tree)\n"
    return context


# ──────────────────────────────────────────────────────────────────────────────
# Plugin state
# ──────────────────────────────────────────────────────────────────────────────

# view.id() -> (PhantomSet, [normalized_lines], common_prefix)
phantoms = {}
# view.id() -> float  (epoch seconds of last request - for 1s debounce)
last_request_time = {}
# view.id() -> (request_id_tuple, threading.Event)
pending_requests = {}
# view.id() set  - prevents on_modified from clearing during Tab-accept
suppress_clear = set()
# view.id() -> float  (grace period end time after Tab-accept)
accept_grace_until = {}


# ──────────────────────────────────────────────────────────────────────────────
# Phantom helpers
# ──────────────────────────────────────────────────────────────────────────────

def show_phantom(view, cursor, suggestion):
    clear_phantoms(view)
    phantom_set = sublime.PhantomSet(view)
    lines = suggestion.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    norm_lines, common_prefix = strip_common_indent(lines)
    preview = "\n".join(norm_lines)
    if not preview:
        return
    phantom = sublime.Phantom(
        sublime.Region(cursor, cursor),
        '<span style="color: gray; font-style: italic;">{0}</span>'.format(
            html.escape(preview)
        ),
        sublime.LAYOUT_INLINE,
    )
    phantom_set.update([phantom])
    phantoms[view.id()] = (phantom_set, norm_lines, common_prefix)
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
        if time.time() < accept_grace_until.get(vid, 0):
            return
        clear_phantoms(view)

    def on_text_command(self, view, command_name, args):
        """Intercept Enter to trigger a suggestion (when appropriate)."""
        if command_name != "insert":
            return None
        if not args or args.get("characters") != "\n":
            return None

        settings      = sublime.load_settings("CodeContinue.sublime-settings")
        trigger_langs = settings.get("trigger_language", [])

        lang_config, ext_key = detect_language(view)
        syntax      = view.syntax()
        syntax_name = syntax.name.lower() if syntax else ""

        lang_matches = (
            not trigger_langs   # empty list means all languages
            or ext_key in [t.lower() for t in trigger_langs]
            or any(t.lower() in syntax_name for t in trigger_langs)
        )
        if not lang_matches:
            return None

        vid = view.id()
        if vid in phantoms:   # suggestion already showing
            return None

        # Debounce: at most one request per second
        now = time.time()
        if now - last_request_time.get(vid, 0) < 1.0:
            return None
        last_request_time[vid] = now

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

        # Detect language
        lang_config, ext_key = detect_language(view)
        _log("Language: {0} (ext={1})".format(lang_config["name"], ext_key))

        # Build prefix (before cursor) and suffix (after cursor)
        max_lines     = settings.get("max_context_lines", 50)
        lines_before  = (max_lines * 2) // 3
        lines_after   = max_lines // 3
        total_lines   = view.rowcol(view.size())[0] + 1
        cursor_row, _ = view.rowcol(cursor)
        start_row     = max(0, cursor_row - lines_before)
        end_row       = min(total_lines, cursor_row + lines_after + 1)
        start_pt      = view.text_point(start_row, 0)
        end_pt        = view.text_point(end_row, 0) if end_row < total_lines else view.size()

        full_code     = view.substr(sublime.Region(start_pt, end_pt))
        cursor_offset = cursor - start_pt
        code_before   = full_code[:cursor_offset]
        code_after    = full_code[cursor_offset:]

        project_context = get_project_context(view.file_name(), max_depth=2)

        # Cancel previous in-flight request
        vid = view.id()
        old = pending_requests.get(vid)
        if old:
            old[1].set()   # signal cancel_event

        cancel_event = threading.Event()
        request_id   = (vid, cursor, time.time())
        pending_requests[vid] = (request_id, cancel_event)

        sublime.status_message("CodeContinue: Fetching …")

        use_fim     = settings.get("use_fim", _is_ollama_generate(endpoint))
        temperature = settings.get("temperature", 0.15)
        top_p       = settings.get("top_p", 0.85)
        max_tokens  = settings.get("max_tokens", 120)
        timeout_s   = settings.get("timeout_ms", 15000) / 1000.0
        stops       = lang_config["stops"]
        headers     = build_api_headers(settings)

        def fetch_completion():
            try:
                if cancel_event.is_set():
                    return

                completion = None

                # ── Ollama FIM path ───────────────────────────────────────────
                if use_fim and _is_ollama_generate(endpoint):
                    prefix = code_before
                    if project_context:
                        prefix = (
                            "# Context:\n# "
                            + project_context.replace("\n", "\n# ")[:500]
                            + "\n\n"
                            + code_before
                        )
                    payload = {
                        "model":  model,
                        "prompt": _build_fim_prompt(prefix, code_after),
                        "stream": False,
                        "options": {
                            "temperature": temperature,
                            "top_p":       top_p,
                            "num_predict": max_tokens,
                            "stop":        stops,
                        },
                    }
                    req = urllib.request.Request(
                        endpoint,
                        data=json.dumps(payload).encode(),
                        headers=headers,
                    )
                    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                        body = json.loads(resp.read().decode())
                        completion = body.get("response", "").strip()

                # ── OpenAI-compatible chat completions path ───────────────────
                else:
                    messages = _build_chat_messages(code_before, code_after, lang_config)
                    if project_context:
                        messages[0]["content"] += (
                            "\n\nProject context:\n" + project_context[:600]
                        )
                    payload = {
                        "model":       model,
                        "messages":    messages,
                        "max_tokens":  max_tokens,
                        "temperature": temperature,
                        "top_p":       top_p,
                    }
                    if stops:
                        payload["stop"] = stops[:4]   # OpenAI allows max 4

                    req = urllib.request.Request(
                        endpoint,
                        data=json.dumps(payload).encode(),
                        headers=headers,
                    )
                    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                        body    = json.loads(resp.read().decode())
                        choices = body.get("choices", [])
                        if choices:
                            completion = (
                                choices[0].get("message", {}).get("content", "")
                                or choices[0].get("text", "")
                            ).strip()

                if cancel_event.is_set():
                    return

                # Check we are still the current (most recent) request
                current = pending_requests.get(vid)
                if not current or current[0] != request_id:
                    return

                if completion:
                    completion = clean_markdown_fences(completion)
                    _log("Suggestion ({0}): {1!r}".format(
                        lang_config["name"], completion[:80]
                    ))
                    sublime.set_timeout(
                        lambda: _present_suggestion(view, cursor, completion), 0
                    )
                else:
                    sublime.set_timeout(
                        lambda: sublime.status_message("CodeContinue: No suggestion."), 0
                    )

            except urllib.error.URLError as exc:
                _log_error("Network error: {0}".format(str(exc)[:120]))
                sublime.set_timeout(
                    lambda: sublime.status_message(
                        "CodeContinue: Network error — check endpoint settings."
                    ), 0
                )
            except Exception as exc:
                _log_error("Unexpected error: {0}".format(str(exc)[:120]))
                sublime.set_timeout(
                    lambda: sublime.status_message("CodeContinue: Error — see console."), 0
                )

        threading.Thread(target=fetch_completion, daemon=True).start()


def _present_suggestion(view, cursor, completion):
    sublime.status_message("")
    show_phantom(view, cursor, completion)


# ──────────────────────────────────────────────────────────────────────────────
# Accept command  (Tab — accepts one line at a time, original behaviour kept)
# ──────────────────────────────────────────────────────────────────────────────

class CodeContinueAcceptCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = self.view
        vid  = view.id()

        if vid not in phantoms:
            return

        phantom_set, remaining, common_prefix = phantoms[vid]
        if not isinstance(remaining, list) or not remaining:
            clear_phantoms(view)
            return

        sel = view.sel()
        if len(sel) != 1:
            clear_phantoms(view)
            return

        suppress_clear.add(vid)
        try:
            insert_pos = sel[0].begin()
            first_line = remaining.pop(0)
            rem_lines  = (
                [common_prefix + ln for ln in remaining]
                if common_prefix else remaining
            )

            text_to_insert = first_line + ("\n" if rem_lines else "")
            view.insert(edit, insert_pos, text_to_insert)

            new_cursor = insert_pos + len(text_to_insert)
            view.sel().clear()
            view.sel().add(sublime.Region(new_cursor, new_cursor))

            if rem_lines:
                preview = "\n".join(rem_lines)
                phantom_set.update([sublime.Phantom(
                    sublime.Region(new_cursor, new_cursor),
                    '<span style="color: gray; font-style: italic;">{0}</span>'.format(
                        html.escape(preview)
                    ),
                    sublime.LAYOUT_INLINE,
                )])
                phantoms[vid] = (phantom_set, rem_lines, "")
            else:
                clear_phantoms(view)
        finally:
            accept_grace_until[vid] = time.time() + 0.25
            suppress_clear.discard(vid)