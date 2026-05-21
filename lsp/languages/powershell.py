"""PowerShell Editor Services handler."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from ..base import LSPSubprocess


class Handler:
    """PSES uses standard LSP; no special workarounds needed."""

    async def after_write(self, server: LSPSubprocess, path: Path, content: str) -> None:
        await server.did_close(path)
        await server.did_open(path, content, version=1)
        await server.did_save(path)

    async def get_diagnostics(self, server: LSPSubprocess, path: Path) -> List[Dict]:
        return []
