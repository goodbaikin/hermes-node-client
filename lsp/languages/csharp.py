"""Roslyn C# LSP handler."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict, List

from ..base import LSPSubprocess
from ..utils import debug_log

logger = logging.getLogger(__name__)


class Handler:
    async def after_initialize(self, server: LSPSubprocess) -> None:
        """Wait for Roslyn project initialization before any didOpen."""
        logger.info("Waiting for Roslyn project initialization...")
        init_ok = await server.wait_for_project_init(timeout=25.0)
        if init_ok:
            logger.info("Roslyn project initialized")
        else:
            logger.warning("Roslyn project init timeout, proceeding anyway")

    async def after_write(self, server: LSPSubprocess, path: Path, content: str) -> None:
        """Roslyn crashes on didClose/didOpen cycle; use notify + didSave instead."""
        uri = path.as_uri()
        if uri not in server._open_documents:
            await server.did_open(path, content, version=1)
        await server.notify_file_changed(path)
        await server.did_save(path)

    async def get_diagnostics(self, server: LSPSubprocess, path: Path) -> List[Dict]:
        """Always try pull diagnostics for Roslyn."""
        diags = server.get_diagnostics(path, timeout=10.0)
        try:
            pull_diags = await asyncio.wait_for(
                server.request_diagnostics(path),
                timeout=10.0,
            )
            if pull_diags:
                diags = pull_diags
                logger.info("Pull diagnostics returned %d items", len(diags))
        except asyncio.TimeoutError:
            logger.warning("Pull diagnostics timed out")
        except Exception as e:
            logger.warning("Pull diagnostics failed: %s", e)
        return diags
