"""model_pool.py — Multi-model, multi-key rotation for CodeContinue.

Supports:
  - Multiple model configs, each with N api_keys
  - Per-key rate-limit cooldown (429 → key goes on cooldown for 60 s)
  - Per-model hard error cooldown (connection errors → model skipped for 30 s)
  - Round-robin key selection within a model
  - Sequential model fallback when all keys of a model are cooling down
  - Cycling: after the last model, wraps back to the first

Thread-safe. Singleton via module-level _pool.
"""

import threading
import time
from typing import Optional


# How long a key is skipped after a 429 (seconds)
KEY_COOLDOWN_S = 60

# How long a model is skipped after a hard connection error (seconds)
MODEL_ERROR_COOLDOWN_S = 30


class _KeyState:
    def __init__(self, key: str):
        self.key        = key
        self.cooldown_until = 0.0   # epoch seconds

    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    def rate_limit(self):
        self.cooldown_until = time.time() + KEY_COOLDOWN_S

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.time())


class _ModelState:
    def __init__(self, cfg: dict):
        self.cfg            = cfg
        self.name: str      = cfg.get("name", cfg.get("model", "unknown"))
        self.enabled: bool  = cfg.get("enabled", True)
        self.error_until    = 0.0
        # Normalise api_keys: accept str or list[str]
        raw = cfg.get("api_keys", cfg.get("api_key", ""))
        if isinstance(raw, str):
            raw = [raw] if raw else []
        self.keys = [_KeyState(k) for k in raw if k]
        self._key_idx = 0
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        if time.time() < self.error_until:
            return False
        return any(k.is_available() for k in self.keys)

    def mark_error(self):
        """Hard error (connection refused, timeout …) → skip model briefly."""
        self.error_until = time.time() + MODEL_ERROR_COOLDOWN_S

    def next_key(self) -> Optional[_KeyState]:
        """Return the next available key in round-robin order, or None."""
        if not self.keys:
            return None
        with self._lock:
            n = len(self.keys)
            for _ in range(n):
                ks = self.keys[self._key_idx % n]
                self._key_idx = (self._key_idx + 1) % n
                if ks.is_available():
                    return ks
        return None


class ModelPool:
    """
    Manages an ordered list of model configs.

    Usage
    -----
    slot = pool.acquire()          # returns a _Slot or raises NoModelsError
    try:
        ... make request with slot.key, slot.endpoint, slot.model_id ...
    except RateLimitError:
        pool.report_429(slot)
    except NetworkError:
        pool.report_error(slot)
    else:
        pool.report_success(slot)
    """

    def __init__(self):
        self._lock   = threading.Lock()
        self._models: list[_ModelState] = []
        self._idx    = 0            # current model pointer

    # ── Configuration ─────────────────────────────────────────────────────────

    def load(self, models_cfg: list[dict]):
        """Replace the pool with a new list of model configs."""
        with self._lock:
            self._models = [_ModelState(c) for c in models_cfg]
            self._idx    = 0

    def model_names(self) -> list[str]:
        with self._lock:
            return [m.name for m in self._models if m.enabled]

    # ── Acquire ───────────────────────────────────────────────────────────────

    def acquire(self) -> "_Slot":
        """
        Return the next usable (model, key) slot.
        Tries all enabled models in order, wrapping around.
        Raises NoModelsError if nothing is available right now.
        """
        with self._lock:
            n = len(self._models)
            if n == 0:
                raise NoModelsError("No models configured.")

            for attempt in range(n):
                ms = self._models[(self._idx + attempt) % n]
                if not ms.enabled:
                    continue
                if time.time() < ms.error_until:
                    continue
                ks = ms.next_key()
                if ks is not None:
                    self._idx = (self._idx + attempt) % n
                    return _Slot(ms, ks)

            # Nothing available — compute how long until first key cools down
            soonest = min(
                (min((k.cooldown_remaining() for k in m.keys), default=0)
                 for m in self._models if m.enabled),
                default=KEY_COOLDOWN_S,
            )
            raise NoModelsError(
                "All models rate-limited. Retry in {0:.0f}s.".format(soonest)
            )

    # ── Feedback ─────────────────────────────────────────────────────────────

    def report_429(self, slot: "_Slot"):
        """Mark the key as rate-limited; advance to the next key/model."""
        slot.key_state.rate_limit()
        # Advance model pointer to encourage trying a different model next time
        with self._lock:
            n = len(self._models)
            if n > 1:
                self._idx = (self._idx + 1) % n

    def report_error(self, slot: "_Slot"):
        """Mark the model as temporarily unavailable (connection/timeout error)."""
        slot.model_state.mark_error()
        with self._lock:
            n = len(self._models)
            if n > 1:
                self._idx = (self._idx + 1) % n

    def report_success(self, slot: "_Slot"):
        """Successful call — keep pointer on this model (it's working)."""
        pass   # nothing to do; pointer stays on current model

    # ── Status ────────────────────────────────────────────────────────────────

    def status_lines(self) -> list[str]:
        """Human-readable status for each model (for debug logging)."""
        lines = []
        now = time.time()
        with self._lock:
            for ms in self._models:
                if not ms.enabled:
                    lines.append("  [{0}] disabled".format(ms.name))
                    continue
                key_statuses = []
                for ks in ms.keys:
                    rem = ks.cooldown_remaining()
                    key_statuses.append(
                        "{0:.0f}s cooldown".format(rem) if rem > 0 else "ok"
                    )
                err_rem = max(0.0, ms.error_until - now)
                if err_rem > 0:
                    lines.append("  [{0}] model error ({1:.0f}s)  keys: {2}".format(
                        ms.name, err_rem, ", ".join(key_statuses)
                    ))
                else:
                    lines.append("  [{0}] keys: {1}".format(
                        ms.name, ", ".join(key_statuses)
                    ))
        return lines


class _Slot:
    """A reserved (model, key) pair returned by ModelPool.acquire()."""

    def __init__(self, model_state: _ModelState, key_state: _KeyState):
        self.model_state = model_state
        self.key_state   = key_state
        cfg              = model_state.cfg
        self.display_name: str = model_state.name
        self.endpoint: str     = cfg.get("endpoint", "")
        self.model_id: str     = cfg.get("model", "")
        self.key: str          = key_state.key
        self.temperature: float = cfg.get("temperature", 0.2)
        self.top_p: float       = cfg.get("top_p", 0.9)
        self.max_tokens: int    = cfg.get("max_tokens", 200)
        self.timeout_s: float   = cfg.get("timeout_ms", 30000) / 1000.0


class NoModelsError(Exception):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

_pool = ModelPool()


def get_pool() -> ModelPool:
    return _pool


def load_pool_from_settings(settings) -> bool:
    """
    Read model configuration from Sublime settings and load the pool.

    Supports two formats:
      New: settings has "models" key (list of model configs)
      Old: settings has "endpoint" + "model" + "api_key" at top level

    Returns True if at least one model was loaded.
    """
    models_cfg = settings.get("models", None)

    if models_cfg and isinstance(models_cfg, list):
        _pool.load(models_cfg)
    else:
        # Backward-compatible single-model format
        endpoint = settings.get("endpoint", "").strip()
        model_id = settings.get("model", "").strip()
        if not endpoint or not model_id:
            return False
        raw_key = settings.get("api_key", "")
        _pool.load([{
            "name":     model_id,
            "enabled":  True,
            "endpoint": endpoint,
            "model":    model_id,
            "api_keys": [raw_key] if isinstance(raw_key, str) else raw_key,
            "temperature": settings.get("temperature", 0.2),
            "top_p":       settings.get("top_p", 0.9),
            "max_tokens":  settings.get("max_tokens", 200),
            "timeout_ms":  settings.get("timeout_ms", 30000),
        }])

    return len(_pool.model_names()) > 0