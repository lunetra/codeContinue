"""symbol_index.py — Lightweight Rust symbol extractor for CodeContinue.

Scans .rs files in the project and extracts a compact, deduplicated index of:
  - Top-level function signatures  (fn name(params) -> ret)
  - Struct names + field names/types
  - Enum names + variants
  - Trait names
  - impl blocks  (impl X / impl Trait for X)  with their method names
  - Type aliases

Design goals:
  - No external dependencies (pure regex, no syn/tree-sitter)
  - Fast: mtime-based cache, async background refresh, <50ms on cache hit
  - Compact output: signatures only, no bodies  (~500-700 chars total)
"""

import os
import re
import threading
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Regex patterns
# ──────────────────────────────────────────────────────────────────────────────

_VIS      = r"(?:pub(?:\s*\(\s*(?:crate|super|in\s+\S+)\s*\)\s*)?\s+)?"
_GENERICS = r"(?:<[^>]*>)?"

# TOP-LEVEL functions only (zero leading whitespace)
_TOP_FN_RE = re.compile(
    r"^" + _VIS + r"(?:async\s+)?fn\s+"
    r"(?P<name>[a-zA-Z_]\w*)" + _GENERICS + r"\s*"
    r"\((?P<params>[^)]*)\)"
    r"(?:\s*->\s*(?P<ret>[^{;/\n]+))?",
    re.MULTILINE,
)

# Methods INSIDE a block (indented by at least one space/tab)
_METHOD_RE = re.compile(
    r"^[ \t]+" + _VIS + r"(?:async\s+)?fn\s+(?P<name>[a-zA-Z_]\w*)",
    re.MULTILINE,
)

# Structs
_STRUCT_RE = re.compile(
    r"^" + _VIS + r"struct\s+(?P<name>[a-zA-Z_]\w*)" + _GENERICS,
    re.MULTILINE,
)
_STRUCT_FIELD_RE = re.compile(
    r"^\s+" + _VIS + r"(?P<fname>[a-zA-Z_]\w*)\s*:\s*(?P<ftype>[^,}\n]+)",
    re.MULTILINE,
)

# Enums
_ENUM_RE = re.compile(
    r"^" + _VIS + r"enum\s+(?P<name>[a-zA-Z_]\w*)" + _GENERICS,
    re.MULTILINE,
)
_VARIANT_RE = re.compile(
    r"^\s+(?P<variant>[A-Z][a-zA-Z0-9_]*)(?:\s*[({](?P<inner>[^)}]*))?",
    re.MULTILINE,
)

# Traits
_TRAIT_RE = re.compile(
    r"^" + _VIS + r"(?:unsafe\s+)?trait\s+(?P<name>[a-zA-Z_]\w*)",
    re.MULTILINE,
)

# Impl blocks
_IMPL_RE = re.compile(
    r"^(?:unsafe\s+)?impl" + _GENERICS + r"\s+"
    r"(?:(?P<trait>[a-zA-Z_][\w:<>, ]*?)\s+for\s+)?"
    r"(?P<type>[a-zA-Z_][\w:<>]*)",
    re.MULTILINE,
)

# Type aliases (with optional generics like Result<T>)
_TYPE_ALIAS_RE = re.compile(
    r"^" + _VIS + r"type\s+(?P<name>[a-zA-Z_]\w*)" + _GENERICS + r"\s*=\s*(?P<alias>[^;]+)",
    re.MULTILINE,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _extract_block(src: str, start_search: int, max_chars: int = 600) -> str:
    """Return the text between the first matching { } after start_search."""
    start = src.find("{", start_search)
    if start == -1:
        return ""
    depth = 0
    for i, ch in enumerate(src[start: start + max_chars]):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1: start + i]
    return ""


def _summarise_params(raw: str) -> str:
    """Collapse 'self' refs and drop default values."""
    if not raw.strip():
        return ""
    parts = []
    for p in raw.split(","):
        p = _clean(p).split("=")[0].strip()
        if p:
            parts.append(p)
    return ", ".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Per-file extraction
# ──────────────────────────────────────────────────────────────────────────────

def extract_symbols(src: str) -> dict:
    """
    Extract Rust symbols from source text.

    Returns dict with keys:
      functions   : list[str]  — top-level fn signatures
      structs     : list[str]  — "struct Name { field: Type, ... }"
      enums       : list[str]  — "enum Name { Var1, Var2(T), ... }"
      traits      : list[str]  — "trait Name"
      impls       : list[str]  — "impl [Trait for] Type { fn1, fn2 }"
      type_aliases: list[str]  — "type Name = ..."
    """
    result: dict = {
        "functions": [],
        "structs": [],
        "enums": [],
        "traits": [],
        "impls": [],
        "type_aliases": [],
    }

    # ── Top-level functions ───────────────────────────────────────────────────
    for m in _TOP_FN_RE.finditer(src):
        name   = m.group("name")
        params = _summarise_params(m.group("params") or "")
        ret    = _clean(m.group("ret") or "").rstrip()
        sig    = "fn {0}({1})".format(name, params)
        if ret:
            sig += " -> " + ret
        result["functions"].append(sig)

    # ── Structs ───────────────────────────────────────────────────────────────
    for m in _STRUCT_RE.finditer(src):
        name  = m.group("name")
        body  = _extract_block(src, m.end(), max_chars=400)
        fields = [
            "{0}: {1}".format(fm.group("fname"), _clean(fm.group("ftype")))
            for fm in _STRUCT_FIELD_RE.finditer(body)
        ]
        if fields:
            result["structs"].append(
                "struct {0} {{ {1} }}".format(name, ", ".join(fields[:6]))
            )
        else:
            result["structs"].append("struct {0}".format(name))

    # ── Enums ─────────────────────────────────────────────────────────────────
    for m in _ENUM_RE.finditer(src):
        name = m.group("name")
        body = _extract_block(src, m.end(), max_chars=300)
        variants = []
        for vm in _VARIANT_RE.finditer(body):
            v = vm.group("variant")
            inner = vm.group("inner")
            if inner:
                v += "({0})".format(_clean(inner)[:40])
            variants.append(v)
        if variants:
            result["enums"].append(
                "enum {0} {{ {1} }}".format(name, ", ".join(variants[:8]))
            )
        else:
            result["enums"].append("enum {0}".format(name))

    # ── Traits ────────────────────────────────────────────────────────────────
    for m in _TRAIT_RE.finditer(src):
        result["traits"].append("trait {0}".format(m.group("name")))

    # ── Impl blocks (with method names from body) ─────────────────────────────
    for m in _IMPL_RE.finditer(src):
        type_name  = m.group("type")
        trait_name = m.group("trait")
        header = (
            "impl {0} for {1}".format(_clean(trait_name), type_name)
            if trait_name else
            "impl {0}".format(type_name)
        )
        body    = _extract_block(src, m.end(), max_chars=800)
        methods = [mm.group("name") for mm in _METHOD_RE.finditer(body)]
        if methods:
            result["impls"].append(
                "{0} {{ {1} }}".format(header, ", ".join(methods[:8]))
            )
        else:
            result["impls"].append(header)

    # ── Type aliases ──────────────────────────────────────────────────────────
    for m in _TYPE_ALIAS_RE.finditer(src):
        result["type_aliases"].append(
            "type {0} = {1}".format(m.group("name"), _clean(m.group("alias"))[:60])
        )

    return result


def format_file_symbols(symbols: dict, filename: str = "") -> str:
    """Format extracted symbols as compact lines for prompt injection."""
    lines = []
    if filename:
        lines.append("// {0}".format(filename))
    for fn_sig in symbols["functions"]:
        lines.append(fn_sig)
    for s in symbols["structs"]:
        lines.append(s)
    for e in symbols["enums"]:
        lines.append(e)
    for t in symbols["traits"]:
        lines.append(t)
    for i in symbols["impls"]:
        lines.append(i)
    for ta in symbols["type_aliases"]:
        lines.append(ta)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Project-level cache
# ──────────────────────────────────────────────────────────────────────────────

class RustSymbolIndex:
    """
    Per-project cache of extracted Rust symbols.

    Thread-safe. Refreshes asynchronously when files change (mtime-based).
    First call on a cold cache returns '' and triggers a background scan;
    subsequent calls are served from cache in <1ms.
    """

    def __init__(self):
        self._lock    = threading.Lock()
        # root_dir -> { "mtime": {path: float}, "symbols": {path: str} }
        self._cache: dict = {}
        self._scanning: set = set()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_context(
        self,
        current_file: Optional[str],
        max_other_files: int = 6,
        max_chars: int = 700,
    ) -> str:
        """
        Return a compact symbol-context string ready for prompt injection.

        Current file: full symbol listing.
        Other files:  one brief summary line each.
        Returns '' if not a Rust file or index not yet ready.
        """
        if not current_file or not current_file.endswith(".rs"):
            return ""

        root = self._find_root(current_file)
        if not root:
            return ""

        self._maybe_refresh(root)

        with self._lock:
            symbols_map: dict = self._cache.get(root, {}).get("symbols", {})

        if not symbols_map:
            return ""

        parts: list = []
        total = 0

        # Current file — full symbols
        cur_text = symbols_map.get(current_file, "")
        if cur_text:
            block = "// {0} (current):\n{1}".format(
                os.path.basename(current_file), cur_text
            )
            parts.append(block)
            total += len(block)

        # Other files — brief one-liner each
        others = [
            (fp, txt) for fp, txt in symbols_map.items()
            if fp != current_file and txt
        ]
        cur_dir = os.path.dirname(current_file)
        others.sort(key=lambda x: (0 if os.path.dirname(x[0]) == cur_dir else 1, x[0]))

        if others and total < max_chars:
            parts.append("// Other files:")
        for fp, sym_text in others[:max_other_files]:
            if total >= max_chars:
                break
            rel   = os.path.relpath(fp, root)
            brief = self._brief(sym_text, rel)
            parts.append(brief)
            total += len(brief)

        return "\n".join(parts)

    def invalidate(self, file_path: str):
        """Force rescan on next get_context call (call on file save)."""
        root = self._find_root(file_path)
        if root:
            self._maybe_refresh(root)

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _find_root(file_path: str) -> Optional[str]:
        d = os.path.dirname(os.path.abspath(file_path))
        for _ in range(6):
            if os.path.isfile(os.path.join(d, "Cargo.toml")):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        return os.path.dirname(os.path.abspath(file_path))

    def _is_stale(self, root: str) -> bool:
        with self._lock:
            cached = self._cache.get(root, {}).get("mtime", {})
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in ("target", ".git") and not d.startswith(".")
                ]
                for fname in filenames:
                    if not fname.endswith(".rs"):
                        continue
                    fp = os.path.join(dirpath, fname)
                    if cached.get(fp) != os.path.getmtime(fp):
                        return True
        except OSError:
            pass
        return False

    def _maybe_refresh(self, root: str):
        with self._lock:
            scanning = root in self._scanning
            cold     = root not in self._cache
        if (cold or self._is_stale(root)) and not scanning:
            threading.Thread(target=self._scan, args=(root,), daemon=True).start()

    def _scan(self, root: str):
        with self._lock:
            self._scanning.add(root)
        try:
            new_mtimes:  dict = {}
            new_symbols: dict = {}
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in ("target", ".git") and not d.startswith(".")
                ]
                for fname in filenames:
                    if not fname.endswith(".rs"):
                        continue
                    fp = os.path.join(dirpath, fname)
                    try:
                        new_mtimes[fp] = os.path.getmtime(fp)
                        with open(fp, encoding="utf-8", errors="ignore") as fh:
                            src = fh.read()
                        syms = extract_symbols(src)
                        text = format_file_symbols(syms)
                        if text.strip():
                            new_symbols[fp] = text
                    except OSError:
                        continue
            with self._lock:
                self._cache[root] = {"mtime": new_mtimes, "symbols": new_symbols}
        finally:
            with self._lock:
                self._scanning.discard(root)

    @staticmethod
    def _brief(sym_text: str, rel_path: str) -> str:
        names = []
        for line in sym_text.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            m = re.match(r"(?:fn|struct|enum|trait|impl)\s+(\w+)", line)
            if m:
                names.append(m.group(1))
        if names:
            return "// {0}: {1}".format(rel_path, ", ".join(names[:6]))
        return "// {0}".format(rel_path)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

_index = RustSymbolIndex()


def get_rust_context(current_file: Optional[str], max_chars: int = 700) -> str:
    """Public API — returns formatted symbol context, or '' if not Rust."""
    return _index.get_context(current_file, max_chars=max_chars)


def on_file_saved(file_path: str):
    """Call from on_post_save listener to invalidate the cache."""
    if file_path and file_path.endswith(".rs"):
        _index.invalidate(file_path)