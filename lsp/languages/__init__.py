"""Language-specific LSP handlers.

Each module exports a Handler class with these optional async methods:
  - on_start(server: LSPSubprocess) -> bool
      Called after subprocess starts, before initialize.
      Return False to abort.
  - after_initialize(server: LSPSubprocess) -> None
      Called after initialize/initialized handshake.
  - before_did_open(server: LSPSubprocess, path: Path, content: str) -> None
      Called before textDocument/didOpen.
  - after_write(server: LSPSubprocess, path: Path, content: str) -> None
      Called after file is written to disk.  Replaces the default
      didClose/didOpen or notify_file_changed logic.
  - get_diagnostics(server: LSPSubprocess, path: Path) -> List[Dict]
      Custom diagnostic fetch.  Return [] to fall back to default.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..base import LSPSubprocess


def load_handler(lang: str):
    """Dynamically load a language handler module."""
    from ..registry import get_config
    cfg = get_config(lang)
    if not cfg or not cfg.handler_module:
        return None
    try:
        mod = importlib.import_module(f"node_client.lsp.languages.{cfg.handler_module}")
        return getattr(mod, "Handler", None)()
    except Exception:
        return None
