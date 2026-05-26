import html
import json
import threading
import time
import urllib.error
import urllib.request
import os
import subprocess

import sublime
import sublime_plugin

from .api import build_api_headers
from .log import _log, _log_error
from .settings import is_endpoint_configured, show_endpoint_config_panel
from .text_utils import clean_markdown_fences, strip_common_indent


# view.id() -> (PhantomSet, [normalized_lines], common_prefix)
phantoms = {}
# view.id() -> seconds since epoch of last suggest request
last_request_time = {}
# view.id() -> request_id
pending_requests = {}
# view.id() set
suppress_clear = set()
accept_grace_until = {}


def get_project_context(file_path, max_depth=2):
    if not file_path:
        return "No file path available."

    file_name = os.path.basename(file_path)
    parent_dir = os.path.dirname(file_path)
    rel_path = os.path.relpath(file_path, parent_dir)

    context = f"File: {file_name}\nPath: {rel_path}\n\n"

    try:
        result = subprocess.run(
            ['tree', '-L', str(max_depth), '-a', '--dirsfirst', '-I', 'target|__pycache__|node_modules|*.lock|*.log'],
            cwd=parent_dir,
            capture_output=True, text=True, timeout=1.2
        )
        if result.returncode == 0:
            context += "Directory Structure:\n" + result.stdout
            return context
    except:
        pass

    context += "Directory Structure (simple):\n"
    try:
        for root, dirs, files in os.walk(parent_dir):
            level = root.replace(parent_dir, '').count(os.sep)
            if level > max_depth:
                continue
            indent = '    ' * level
            context += f"{indent}├── {os.path.basename(root)}/\n"
            if level < max_depth:
                for f in sorted(files)[:15]:
                    context += f"{indent}│   └── {f}\n"
    except:
        context += "Could not generate directory tree.\n"

    return context


class CodeContinueListener(sublime_plugin.EventListener):
    def on_modified(self, view):
        vid = view.id()
        if vid in phantoms:
            if vid in suppress_clear:
                return
            grace = accept_grace_until.get(vid, 0)
            if time.time() < grace:
                return
            clear_phantoms(view)
        return

    def on_text_command(self, view, command_name, args):
        settings = sublime.load_settings("CodeContinue.sublime-settings")
        trigger_langs = settings.get("trigger_language", [])
        if not view.syntax():
            return
        if view.syntax().name.lower() not in [lang.lower() for lang in trigger_langs]:
            return

        if command_name == "insert" and args and args.get("characters") == "\n":
            existing = phantoms.get(view.id())
            if existing and isinstance(existing[1], list) and len(existing[1]) > 0:
                return
            vid = view.id()
            if vid in phantoms:
                return
            now = time.time()
            last = last_request_time.get(vid, 0)
            if now - last < 1.0:
                return
            last_request_time[vid] = now
            sublime.set_timeout(lambda: view.run_command("code_continue_suggest"), 50)


class CodeContinueSuggestCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        settings = sublime.load_settings("CodeContinue.sublime-settings")
        endpoint = settings.get("endpoint", "")
        model = settings.get("model", "")
        max_lines = settings.get("max_context_lines", 50)

        if not is_endpoint_configured(settings):
            sublime.status_message("CodeContinue: Endpoint not configured.")
            sublime.set_timeout(lambda: show_endpoint_config_panel(view), 100)
            return

        sel = view.sel()
        if len(sel) != 1:
            return
        cursor = sel[0].begin()

        file_path = view.file_name()
        project_context = get_project_context(file_path, max_depth=2)

        cursor_row, _ = view.rowcol(cursor)
        lines_before = max_lines // 2
        lines_after = max_lines // 2

        total_lines = view.rowcol(view.size())[0] + 1
        start_row = max(0, cursor_row - lines_before)
        end_row = min(total_lines, cursor_row + lines_after + 1)

        start_point = view.text_point(start_row, 0)
        end_point = view.text_point(end_row, 0) if end_row < total_lines else view.size()
        code = view.substr(sublime.Region(start_point, end_point))

        cursor_offset = cursor - start_point
        code_before = code[:cursor_offset]

        prompt = f"""{project_context}

Current code (around cursor):
```rust
{code_before}
```

Continue the Rust code naturally from the cursor position.
- Follow Rust idioms and best practices.
- Only output the code continuation.
- Do not repeat existing code.
- Do not add explanations, markdown, or comments."""

        vid = view.id()
        request_id = (vid, cursor, time.time())
        pending_requests[vid] = request_id

        sublime.status_message("CodeContinue: Fetching suggestion...")

        def fetch_completion():
            try:
                if pending_requests.get(vid) != request_id:
                    return

                data = {
                    "model": model,
                    "messages": [
                        {
                            "role": "system", 
                            "content": "You are an expert Rust developer. Always write clean, idiomatic Rust code. Output ONLY the code continuation. Never add explanations, markdown, backticks, or comments."
                        },
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 512,
                    "temperature": 0.2
                }

                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(data).encode(),
                    headers=build_api_headers(settings),
                )

                with urllib.request.urlopen(req, timeout=12) as response:
                    result = json.loads(response.read().decode())
                    completion = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    completion = clean_markdown_fences(completion)

                    if pending_requests.get(vid) == request_id and completion:
                        sublime.set_timeout(lambda: show_phantom(view, cursor, completion), 0)

            except Exception as e:
                _log_error(f"Error: {str(e)[:150]}")
                sublime.set_timeout(lambda: sublime.status_message("CodeContinue: Error fetching suggestion"), 0)

        threading.Thread(target=fetch_completion, daemon=True).start()


class CodeContinueAcceptCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        vid = view.id()
        if vid not in phantoms:
            return

        phantom_set, remaining, common_prefix = phantoms[vid]
        if not isinstance(remaining, list) or len(remaining) == 0:
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
            rem_lines = [common_prefix + ln for ln in remaining] if common_prefix else remaining

            text_to_insert = first_line
            if rem_lines:
                text_to_insert += "\n"

            view.insert(edit, insert_pos, text_to_insert)

            new_cursor = insert_pos + len(text_to_insert)
            view.sel().clear()
            view.sel().add(sublime.Region(new_cursor, new_cursor))

            if rem_lines:
                preview = "\n".join(rem_lines)
                phantom_set.update([sublime.Phantom(
                    sublime.Region(new_cursor, new_cursor),
                    '<span style="color: gray">{0}</span>'.format(html.escape(preview)),
                    sublime.LAYOUT_INLINE,
                )])
                phantoms[vid] = (phantom_set, rem_lines, "")
            else:
                clear_phantoms(view)
        finally:
            accept_grace_until[vid] = time.time() + 0.25
            if vid in suppress_clear:
                suppress_clear.remove(vid)


def show_phantom(view, cursor, suggestion):
    clear_phantoms(view)
    phantom_set = sublime.PhantomSet(view)

    lines = suggestion.split('\n')
    if lines and lines[-1] == "":
        lines = lines[:-1]

    norm_lines, common_prefix = strip_common_indent(lines)
    preview = "\n".join(norm_lines)

    if not preview:
        return

    phantom = sublime.Phantom(
        sublime.Region(cursor, cursor),
        '<span style="color: gray">{0}</span>'.format(html.escape(preview)),
        sublime.LAYOUT_INLINE,
    )
    phantom_set.update([phantom])
    phantoms[view.id()] = (phantom_set, norm_lines, common_prefix)
    view.set_status('code_continue_visible', 'true')


def clear_phantoms(view):
    vid = view.id()
    if vid in phantoms:
        phantoms[vid][0].update([])
        del phantoms[vid]
    view.erase_status('code_continue_visible')
