"""symbol_index.py — Compact Rust symbol extractor for CodeContinue.

Two sources:
  1. Current file  — extracted SYNCHRONOUSLY from view content (always fresh)
  2. Neighboring files — same directory only, cached by mtime, optional

Output is intentionally minimal so small models (3b/7b) aren't overwhelmed:
  Current:   "fn foo(x: i32) -> bool | struct Bar { x, y } | enum Baz { A, B(T) }"
  Neighbors: "models.rs: User, Session, Role | db.rs: connect, query"
"""

import os
import re
import threading
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Regex (top-level only — no leading whitespace)
# ──────────────────────────────────────────────────────────────────────────────

_VIS = r"(?:pub(?:\s*\([^)]*\))?\s+)?"

_TOP_FN_RE = re.compile(
    r"^" + _VIS + r"(?:async\s+)?fn\s+(?P<name>[a-zA-Z_]\w*)(?:<[^>]*>)?\s*"
    r"\((?P<params>[^)]*)\)(?:\s*->\s*(?P<ret>[^{;\n]+))?",
    re.MULTILINE,
)
_METHOD_RE = re.compile(
    r"^[ \t]+" + _VIS + r"(?:async\s+)?fn\s+(?P<name>[a-zA-Z_]\w*)",
    re.MULTILINE,
)
_STRUCT_RE = re.compile(
    r"^" + _VIS + r"struct\s+(?P<name>[a-zA-Z_]\w*)(?:<[^>]*>)?",
    re.MULTILINE,
)
_STRUCT_FIELD_RE = re.compile(
    r"^\s+" + _VIS + r"(?P<fname>[a-zA-Z_]\w*)\s*:\s*(?P<ftype>[^,}\n]+)",
    re.MULTILINE,
)
_ENUM_RE = re.compile(
    r"^" + _VIS + r"enum\s+(?P<name>[a-zA-Z_]\w*)(?:<[^>]*>)?",
    re.MULTILINE,
)
_VARIANT_RE = re.compile(
    r"^\s+(?P<v>[A-Z][a-zA-Z0-9_]*)(?:\s*[({](?P<inner>[^)}]*))?",
    re.MULTILINE,
)
_TRAIT_RE = re.compile(
    r"^" + _VIS + r"(?:unsafe\s+)?trait\s+(?P<name>[a-zA-Z_]\w*)",
    re.MULTILINE,
)
_IMPL_RE = re.compile(
    r"^(?:unsafe\s+)?impl(?:<[^>]*>)?\s+"
    r"(?:(?P<trait>[a-zA-Z_][\w:<>, ]*?)\s+for\s+)?"
    r"(?P<type>[a-zA-Z_][\w:<>]*)",
    re.MULTILINE,
)


def _c(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def _block(src, pos, maxc=500):
    start = src.find("{", pos)
    if start == -1:
        return ""
    depth = 0
    for i, ch in enumerate(src[start: start + maxc]):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1: start + i]
    return ""


def _params(raw):
    parts = []
    for p in (raw or "").split(","):
        p = _c(p).split("=")[0].strip()
        if p:
            parts.append(p)
    return ", ".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Extraction
# ──────────────────────────────────────────────────────────────────────────────

def extract_symbols(src: str) -> dict:
    """Extract top-level Rust symbols from source text."""
    out = {"functions": [], "structs": [], "enums": [],
           "traits": [], "impls": []}

    for m in _TOP_FN_RE.finditer(src):
        sig = "fn {0}({1})".format(m.group("name"), _params(m.group("params")))
        ret = _c(m.group("ret") or "").rstrip()
        if ret:
            sig += " -> " + ret
        out["functions"].append(sig)

    for m in _STRUCT_RE.finditer(src):
        body = _block(src, m.end(), 300)
        fields = [fm.group("fname") for fm in _STRUCT_FIELD_RE.finditer(body)]
        if fields:
            out["structs"].append("struct {0} {{{1}}}".format(
                m.group("name"), ", ".join(fields[:5])
            ))
        else:
            out["structs"].append("struct {0}".format(m.group("name")))

    for m in _ENUM_RE.finditer(src):
        body = _block(src, m.end(), 200)
        variants = []
        for vm in _VARIANT_RE.finditer(body):
            v = vm.group("v")
            inner = vm.group("inner")
            if inner:
                v += "({0})".format(_c(inner)[:30])
            variants.append(v)
        if variants:
            out["enums"].append("enum {0} {{{1}}}".format(
                m.group("name"), ", ".join(variants[:6])
            ))
        else:
            out["enums"].append("enum {0}".format(m.group("name")))

    for m in _TRAIT_RE.finditer(src):
        out["traits"].append("trait {0}".format(m.group("name")))

    for m in _IMPL_RE.finditer(src):
        body    = _block(src, m.end(), 600)
        methods = [mm.group("name") for mm in _METHOD_RE.finditer(body)]
        typ     = m.group("type")
        trait   = m.group("trait")
        header  = "impl {0} for {1}".format(_c(trait), typ) if trait else "impl {0}".format(typ)
        if methods:
            out["impls"].append("{0} {{{1}}}".format(header, ", ".join(methods[:6])))
        else:
            out["impls"].append(header)

    return out


def format_compact(symbols: dict, max_chars: int = 300) -> str:
    """
    Format symbols as a single compact line (or a few lines) for model context.
    Very short so small models aren't overwhelmed.

    Example output:
      fn fetch(url: &str) -> Result<Vec<u8>> | fn parse(data: &[u8]) -> HashMap
      struct Config {host, port, timeout} | enum Status {Active, Pending(String)}
      impl Config {new, validate} | impl Validator for Config {validate}
    """
    parts = []
    parts.extend(symbols["functions"])
    parts.extend(symbols["structs"])
    parts.extend(symbols["enums"])
    parts.extend(symbols["traits"])
    parts.extend(symbols["impls"])

    lines = []
    row = []
    row_len = 0
    for p in parts:
        if row_len + len(p) + 3 > 100:          # soft wrap at 100 chars / line
            lines.append(" | ".join(row))
            row = [p]
            row_len = len(p)
        else:
            row.append(p)
            row_len += len(p) + 3
    if row:
        lines.append(" | ".join(row))

    result = "\n".join(lines)
    return result[:max_chars]


# ──────────────────────────────────────────────────────────────────────────────
# Neighbor file cache  (same directory only, mtime-based)
# ──────────────────────────────────────────────────────────────────────────────

class _NeighborCache:
    def __init__(self):
        self._lock    = threading.Lock()
        # dir -> { filepath -> (mtime, compact_str) }
        self._cache: dict = {}
        self._scanning: set = set()

    def get(self, current_file: str) -> str:
        """Return a one-liner summary of neighbor .rs files in the same dir."""
        d = os.path.dirname(os.path.abspath(current_file))
        self._maybe_refresh(d, current_file)
        with self._lock:
            entries = self._cache.get(d, {})
        parts = []
        for fp, (_, compact) in entries.items():
            if fp == current_file or not compact:
                continue
            name = os.path.basename(fp)
            # Collect just the identifiers (first word after fn/struct/enum/impl)
            idents = []
            for line in compact.splitlines():
                m = re.search(r"(?:fn|struct|enum|trait|impl)\s+(\w+)", line)
                if m:
                    idents.append(m.group(1))
            if idents:
                parts.append("{0}: {1}".format(name, ", ".join(dict.fromkeys(idents))[:60]))
        return "\n".join(parts[:6])   # max 6 neighbor files

    def invalidate(self, file_path: str):
        d = os.path.dirname(os.path.abspath(file_path))
        with self._lock:
            if d in self._cache and file_path in self._cache[d]:
                del self._cache[d][file_path]
        self._maybe_refresh(d, file_path)

    def _maybe_refresh(self, d: str, current_file: str):
        with self._lock:
            scanning = d in self._scanning
        stale = self._is_stale(d)
        if stale and not scanning:
            t = threading.Thread(target=self._scan, args=(d,), daemon=True)
            t.start()

    def _is_stale(self, d: str) -> bool:
        with self._lock:
            cached = self._cache.get(d, {})
        try:
            for fname in os.listdir(d):
                if not fname.endswith(".rs"):
                    continue
                fp = os.path.join(d, fname)
                if cached.get(fp, (None,))[0] != os.path.getmtime(fp):
                    return True
        except OSError:
            pass
        return not bool(cached)   # cold cache → stale

    def _scan(self, d: str):
        with self._lock:
            self._scanning.add(d)
        try:
            result = {}
            for fname in os.listdir(d):
                if not fname.endswith(".rs"):
                    continue
                fp = os.path.join(d, fname)
                try:
                    mtime = os.path.getmtime(fp)
                    with open(fp, encoding="utf-8", errors="ignore") as fh:
                        src = fh.read()
                    syms    = extract_symbols(src)
                    compact = format_compact(syms, max_chars=200)
                    result[fp] = (mtime, compact)
                except OSError:
                    continue
            with self._lock:
                self._cache[d] = result
        finally:
            with self._lock:
                self._scanning.discard(d)


_neighbor_cache = _NeighborCache()


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def get_symbol_context(
    current_file: Optional[str],
    view_content: str,
    index_neighbors: bool = True,
    max_chars: int = 400,
) -> str:
    """
    Return compact symbol context for injection into the model prompt.

    current_file  : absolute path to the file being edited
    view_content  : full text of the current view (for synchronous extraction)
    index_neighbors: if False, only current file symbols are returned
    max_chars     : total character budget

    Format:
      // current:
      fn foo(x: i32) -> bool | struct Bar {x, y} | enum Baz {A, B(T)}
      // neighbors:
      models.rs: User, Session | db.rs: connect, query
    """
    if not current_file or not current_file.endswith(".rs"):
        return ""

    parts = []

    # 1. Current file — synchronous, always fresh
    cur_syms    = extract_symbols(view_content)
    cur_compact = format_compact(cur_syms, max_chars=max_chars // 2)
    if cur_compact:
        parts.append("// current file:\n" + cur_compact)

    # 2. Neighboring files — async cache, same dir only
    if index_neighbors:
        neighbor_text = _neighbor_cache.get(current_file)
        if neighbor_text:
            parts.append("// neighbors:\n" + neighbor_text)

    result = "\n".join(parts)
    return result[:max_chars]


def on_file_saved(file_path: str):
    """Invalidate cache when a .rs file is saved."""
    if file_path and file_path.endswith(".rs"):
        _neighbor_cache.invalidate(file_path)