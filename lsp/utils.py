"""LSP utilities — debug logging, path conversion, diagnostic compaction."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Debug log file for tracing LSP messages — controlled by HERMES_LSP_DEBUG env var.
_debug_log_path = Path.home() / ".hermes" / "logs" / "lsp" / "_debug_rpc.log"
_debug_enabled = os.environ.get("HERMES_LSP_DEBUG", "").strip() in ("1", "true", "yes")


def debug_log(msg: str) -> None:
    if not _debug_enabled:
        return
    try:
        _debug_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_debug_log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def path_to_uri(path: str) -> str:
    if path.startswith("file://"):
        return path
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return "file://" + path.replace("\\", "/")
    return path


def compact_diag(d: Dict) -> Dict:
    """Strip large fields from diagnostic to keep WebSocket message small."""
    return {
        "code": d.get("code"),
        "message": d.get("message", ""),
        "severity": d.get("severity", 1),
        "range": d.get("range", {}),
    }


def diagnostic_key(d: Dict) -> tuple:
    """Key for delta comparison: (code, start_line)."""
    return (
        d.get("code"),
        d.get("range", {}).get("start", {}).get("line"),
    )


def compute_delta(current: List[Dict], baseline: List[Dict]) -> List[Dict]:
    """Return diagnostics in current that are not in baseline."""
    if not baseline:
        return list(current)
    baseline_keys = {diagnostic_key(d) for d in baseline}
    return [d for d in current if diagnostic_key(d) not in baseline_keys]
