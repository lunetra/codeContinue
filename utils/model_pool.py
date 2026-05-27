"""model_pool.py — Multi-model, multi-key rotation for CodeContinue."""

import threading
import time
from typing import Dict, List, Optional

KEY_COOLDOWN_S       = 60
MODEL_ERROR_COOLDOWN = 30


class _KeyState:
    def __init__(self, key: str):
        self.key             = key
        self.cooldown_until  = 0.0

    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    def rate_limit(self):
        self.cooldown_until = time.time() + KEY_COOLDOWN_S

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.time())


class _ModelState:
    def __init__(self, cfg: dict):
        self.cfg           = cfg
        self.name: str     = cfg.get("name", cfg.get("model", "unknown"))
        self.enabled: bool = cfg.get("enabled", True)
        self.error_until   = 0.0
        raw = cfg.get("api_keys", cfg.get("api_key", ""))
        if isinstance(raw, str):
            raw = [raw] if raw else []
        self.keys          = [_KeyState(k) for k in raw if k]
        self._key_idx      = 0
        self._lock         = threading.Lock()

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        if time.time() < self.error_until:
            return False
        return any(k.is_available() for k in self.keys)

    def mark_error(self):
        self.error_until = time.time() + MODEL_ERROR_COOLDOWN

    def next_key(self) -> Optional[_KeyState]:
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
    def __init__(self):
        self._lock    = threading.Lock()
        self._models: List[_ModelState] = []
        self._idx     = 0

    def load(self, models_cfg: List[dict]):
        with self._lock:
            self._models = [_ModelState(c) for c in models_cfg]
            self._idx    = 0

    def model_names(self) -> List[str]:
        with self._lock:
            return [m.name for m in self._models if m.enabled]

    def acquire(self) -> "_Slot":
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
            soonest = min(
                (min((k.cooldown_remaining() for k in m.keys), default=0)
                 for m in self._models if m.enabled),
                default=KEY_COOLDOWN_S,
            )
            raise NoModelsError(
                "All models rate-limited. Retry in {0:.0f}s.".format(soonest)
            )

    def report_429(self, slot: "_Slot"):
        """Mark THIS key as cooling down. Do NOT jump to next model —
        acquire() will automatically try the next available key of the
        same model, only moving to the next model when all keys are busy."""
        slot.key_state.rate_limit()

    def report_error(self, slot: "_Slot"):
        slot.model_state.mark_error()
        with self._lock:
            n = len(self._models)
            if n > 1:
                self._idx = (self._idx + 1) % n

    def report_success(self, slot: "_Slot"):
        pass

    def status_lines(self) -> List[str]:
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
                    key_statuses.append("{0:.0f}s".format(rem) if rem > 0 else "ok")
                err_rem = max(0.0, ms.error_until - now)
                if err_rem > 0:
                    lines.append("  [{0}] error({1:.0f}s) keys:{2}".format(
                        ms.name, err_rem, ",".join(key_statuses)))
                else:
                    lines.append("  [{0}] keys:{1}".format(
                        ms.name, ",".join(key_statuses)))
        return lines


class _Slot:
    def __init__(self, model_state: _ModelState, key_state: _KeyState):
        self.model_state    = model_state
        self.key_state      = key_state
        cfg                 = model_state.cfg
        self.display_name   = model_state.name
        self.endpoint: str  = cfg.get("endpoint", "")
        self.model_id: str  = cfg.get("model", "")
        self.key: str       = key_state.key
        self.temperature    = cfg.get("temperature", 0.2)
        self.top_p          = cfg.get("top_p", 0.9)
        self.max_tokens     = cfg.get("max_tokens", 120)
        self.timeout_s      = cfg.get("timeout_ms", 30000) / 1000.0
        # FIM support: if endpoint contains /api/generate, use FIM mode
        self.use_fim        = cfg.get("use_fim", "/api/generate" in self.endpoint)

    def key_index(self) -> int:
        """1-based index of this key in the model's key list."""
        try:
            return self.model_state.keys.index(self.key_state) + 1
        except ValueError:
            return 1


class NoModelsError(Exception):
    pass


_pool = ModelPool()


def get_pool() -> ModelPool:
    return _pool


def load_pool_from_settings(settings) -> bool:
    models_cfg = settings.get("models", None)
    if models_cfg and isinstance(models_cfg, list):
        _pool.load(models_cfg)
    else:
        endpoint = settings.get("endpoint", "").strip()
        model_id = settings.get("model", "").strip()
        if not endpoint or not model_id:
            return False
        raw_key = settings.get("api_key", "")
        _pool.load([{
            "name":        model_id,
            "enabled":     True,
            "endpoint":    endpoint,
            "model":       model_id,
            "api_keys":    [raw_key] if isinstance(raw_key, str) else raw_key,
            "temperature": settings.get("temperature", 0.2),
            "top_p":       settings.get("top_p", 0.9),
            "max_tokens":  settings.get("max_tokens", 120),
            "timeout_ms":  settings.get("timeout_ms", 30000),
        }])
    return len(_pool.model_names()) > 0