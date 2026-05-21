"""PowerShell LSP handler — uses PSScriptAnalyzer directly."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List

from ..base import LSPSubprocess

logger = logging.getLogger(__name__)


class Handler:
    """PSScriptAnalyzer direct execution (PSES has stdio bugs on Windows)."""

    async def after_write(self, server: LSPSubprocess, path: Path, content: str) -> None:
        # Write file to disk for PSScriptAnalyzer
        path.write_text(content, encoding="utf-8")

    async def get_diagnostics(self, server: LSPSubprocess, path: Path) -> List[Dict]:
        """Run PSScriptAnalyzer directly and convert to LSP diagnostic format."""
        import subprocess
        import asyncio

        try:
            # Run PSScriptAnalyzer via pwsh
            proc = await asyncio.create_subprocess_exec(
                "pwsh", "-Command",
                f"Invoke-ScriptAnalyzer -Path '{path}' | ConvertTo-Json -Depth 10",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)

            if proc.returncode != 0:
                logger.warning("PSScriptAnalyzer error: %s", stderr.decode("utf-8", errors="replace")[:200])
                return []

            output = stdout.decode("utf-8", errors="replace").strip()
            if not output:
                return []

            # Parse JSON output
            try:
                results = json.loads(output)
            except json.JSONDecodeError:
                logger.warning("PSScriptAnalyzer returned non-JSON: %s", output[:200])
                return []

            # Ensure list
            if isinstance(results, dict):
                results = [results]

            # Convert to LSP diagnostic format
            diags = []
            for r in results:
                severity_map = {
                    "Information": 3,
                    "Warning": 2,
                    "Error": 1,
                }
                line = r.get("Line", 1) - 1  # 1-based to 0-based
                diags.append({
                    "code": r.get("RuleName", "PSAnalyzer"),
                    "message": r.get("Message", ""),
                    "severity": severity_map.get(r.get("Severity", "Warning"), 2),
                    "range": {
                        "start": {"line": line, "character": r.get("Column", 1) - 1},
                        "end": {"line": line, "character": 999},
                    },
                })

            logger.info("PSScriptAnalyzer returned %d diagnostics", len(diags))
            return diags

        except asyncio.TimeoutError:
            logger.warning("PSScriptAnalyzer timed out")
            return []
        except Exception as e:
            logger.warning("PSScriptAnalyzer failed: %s", e)
            return []
