"""Diagnostic caching, baseline storage, and delta computation."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List

from .utils import compact_diag, compute_delta, debug_log


class DiagnosticStore:
    """Thread-safe diagnostic cache with persistent baselines."""

    def __init__(self):
        self._lock = threading.Lock()
        self._diagnostics: Dict[str, List[Dict]] = {}   # uri -> diags
        self._baselines: Dict[str, List[Dict]] = {}      # key -> diags

    # -- live diagnostics -----------------------------------------------------

    def set(self, uri: str, diags: List[Dict]) -> None:
        with self._lock:
            self._diagnostics[uri] = diags

    def get(self, uri: str) -> List[Dict]:
        with self._lock:
            return list(self._diagnostics.get(uri, []))

    # -- baselines ------------------------------------------------------------

    def _baseline_key(self, lang: str, workspace_root: str, file_path: str) -> str:
        root = Path(workspace_root).resolve().as_posix()
        uri = Path(file_path).resolve().as_uri()
        return f"{lang}:{root}:{uri}"

    def store_baseline(self, lang: str, workspace_root: str, file_path: str, diags: List[Dict]) -> None:
        key = self._baseline_key(lang, workspace_root, file_path)
        with self._lock:
            self._baselines[key] = list(diags)
        debug_log(f"[baseline_store] stored {len(diags)} diags for {key}")

    def load_baseline(self, lang: str, workspace_root: str, file_path: str) -> List[Dict]:
        key = self._baseline_key(lang, workspace_root, file_path)
        with self._lock:
            diags = list(self._baselines.get(key, []))
        debug_log(f"[baseline_load] loaded {len(diags)} diags for {key}")
        return diags

    def clear_baseline(self, lang: str, workspace_root: str, file_path: str) -> None:
        key = self._baseline_key(lang, workspace_root, file_path)
        with self._lock:
            self._baselines.pop(key, None)
        debug_log(f"[baseline_clear] cleared {key}")

    # -- delta ----------------------------------------------------------------

    def delta(self, lang: str, workspace_root: str, file_path: str, current: List[Dict]) -> List[Dict]:
        baseline = self.load_baseline(lang, workspace_root, file_path)
        if not baseline:
            return list(current)
        result = compute_delta(current, baseline)
        debug_log(f"[delta] {len(result)} new diags (current={len(current)}, baseline={len(baseline)})")
        return result

    def compact(self, diags: List[Dict]) -> List[Dict]:
        return [compact_diag(d) for d in diags]
