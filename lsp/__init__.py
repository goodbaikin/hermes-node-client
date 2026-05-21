"""LSP RPC Server for hermes_node_client.

Provides language-server diagnostics via JSON-RPC over stdio,
exposed through the existing node_client HTTP/WebSocket protocol.

Each language gets a dedicated LSP subprocess.  Diagnostics are
pushed by the server, cached here, and returned on request.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import LSPSubprocess
from .diagnostics import DiagnosticStore
from .languages import load_handler
from .registry import get_config
from .utils import compact_diag, compute_delta, debug_log

logger = logging.getLogger(__name__)


class LSPServerManager:
    """Manages multiple LSP subprocesses keyed by (language, workspace_root)."""

    _servers: Dict[str, Any] = {}
    _servers_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.Lock()
        self._server_locks: Dict[str, asyncio.Lock] = {}
        self._server_locks_lock = threading.Lock()
        self._store = DiagnosticStore()

    def _get_server_lock(self, key: str) -> asyncio.Lock:
        with self._server_locks_lock:
            if key not in self._server_locks:
                self._server_locks[key] = asyncio.Lock()
            return self._server_locks[key]

    async def get_or_create(self, lang: str, workspace_root: str) -> Optional[LSPSubprocess]:
        root_path = Path(workspace_root).resolve()
        key = f"{lang}:{root_path.as_posix()}"
        debug_log(f"[get_or_create] key={key}")
        with self._lock:
            server = self._servers.get(key)
            if server is not None and server.is_alive():
                debug_log(f"[get_or_create] found existing healthy server for {key}")
                return server
            if server is not None and not server.is_alive():
                debug_log(f"[get_or_create] removing dead server for {key}")
                try:
                    if server.process and server.process.returncode is None:
                        server.process.kill()
                except Exception:
                    pass
                self._servers.pop(key, None)

        debug_log(f"[get_or_create] creating new server for {key}")
        server = LSPSubprocess(lang, root_path)
        ok = await server.start()
        if not ok:
            return None

        # Run language-specific post-init hook
        handler = load_handler(lang)
        if handler and hasattr(handler, "after_initialize"):
            await handler.after_initialize(server)

        with self._lock:
            self._servers[key] = server
        return server

    async def handle_request(self, request: Dict) -> Dict:
        action = request.get("action")
        lang = request.get("language")
        workspace_root = request.get("workspace_root", str(Path.cwd()))
        file_path = request.get("file_path")
        content = request.get("content", "")
        use_delta = request.get("delta", False)

        debug_log(
            f"[handle_request] action={action}, lang={lang}, "
            f"workspace_root={workspace_root}, file_path={file_path}, delta={use_delta}"
        )

        root_path = Path(workspace_root).resolve()
        key = f"{lang}:{root_path.as_posix()}"
        server_lock = self._get_server_lock(key)

        async with server_lock:
            if action == "lint_after_write":
                return await self._lint_after_write(lang, workspace_root, file_path, content, use_delta)
            if action == "snapshot_baseline":
                return await self._snapshot_baseline(lang, workspace_root, file_path, content)
            if action == "get_diagnostics":
                return await self._get_diagnostics(lang, workspace_root, file_path, use_delta)
            if action == "shutdown":
                await self.shutdown_all()
                return {"status": "ok"}
            return {"error": f"unknown action: {action}"}

    # -- actions --------------------------------------------------------------

    async def _lint_after_write(self, lang: str, workspace_root: str, file_path: str, content: str, use_delta: bool = False) -> Dict:
        root = Path(workspace_root).resolve()
        cfg = get_config(lang)

        # Ensure workspace has a marker file so LSP servers recognize it
        if cfg and cfg.root_markers:
            has_marker = any((root / m).exists() for m in cfg.root_markers)
            if not has_marker and cfg.root_markers:
                marker = root / cfg.root_markers[0]
                if not marker.exists():
                    _create_marker(marker, cfg.root_markers[0])

        server = await self.get_or_create(lang, workspace_root)
        if server is None:
            return {"error": f"Failed to start LSP for {lang}"}

        path = Path(file_path).resolve()

        # Write content to actual file so server can detect changes
        try:
            path.write_text(content, encoding="utf-8")
            logger.info("Wrote %d bytes to %s", len(content), path)
        except Exception as e:
            logger.warning("Failed to write file: %s", e)

        # Language-specific post-write handling
        handler = load_handler(lang)
        if handler and hasattr(handler, "after_write"):
            await handler.after_write(server, path, content)
        else:
            # Default: close/reopen + save
            await server.did_close(path)
            await server.did_open(path, content, version=1)
            await server.did_save(path)

        # Wait for diagnostics
        wait_seconds = cfg.wait_seconds if cfg else 5.0
        logger.info("Waiting %.1fs for %s diagnostics...", wait_seconds, lang)
        await asyncio.sleep(wait_seconds)

        # Language-specific diagnostic fetch
        diags: List[Dict] = []
        if handler and hasattr(handler, "get_diagnostics"):
            diags = await handler.get_diagnostics(server, path)
        if not diags:
            diags = server.get_diagnostics(path, timeout=10.0)
            if not diags:
                try:
                    pull_diags = await asyncio.wait_for(
                        server.request_diagnostics(path), timeout=10.0
                    )
                    if pull_diags:
                        diags = pull_diags
                        logger.info("Pull diagnostics returned %d items", len(diags))
                except asyncio.TimeoutError:
                    logger.warning("Pull diagnostics timed out")
                except Exception as e:
                    logger.warning("Pull diagnostics failed: %s", e)

        if use_delta:
            diags = self._store.delta(lang, workspace_root, file_path, diags)
            logger.info("Delta filtering: %d diagnostics after filtering", len(diags))

        result = {"diagnostics": diags}
        log_path = server.get_log_path()
        if log_path:
            result["lsp_log"] = str(log_path)
        return result

    async def _snapshot_baseline(self, lang: str, workspace_root: str, file_path: str, content: str = "") -> Dict:
        server = await self.get_or_create(lang, workspace_root)
        if server is None:
            return {"error": f"Failed to start LSP for {lang}"}
        path = Path(file_path).resolve()
        if not content:
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                pass

        cfg = get_config(lang)
        if cfg and cfg.handler_module:
            handler = load_handler(lang)
            if handler and hasattr(handler, "after_initialize"):
                await handler.after_initialize(server)

        uri = path.as_uri()
        if uri not in server._open_documents:
            await server.did_open(path, content, version=1)
        wait_seconds = cfg.wait_seconds if cfg else 5.0
        await asyncio.sleep(wait_seconds)

        baseline_diags: List[Dict] = []
        try:
            baseline_diags = await asyncio.wait_for(
                server.request_diagnostics(path), timeout=10.0
            )
            logger.info("Baseline pull diagnostics: %d items", len(baseline_diags))
        except Exception:
            pass

        push_diags = server.get_diagnostics(path, timeout=2.0)
        if push_diags and not baseline_diags:
            baseline_diags = push_diags
            logger.info("Using push diagnostics for baseline: %d items", len(baseline_diags))

        self._store.store_baseline(lang, workspace_root, file_path, baseline_diags)
        with server._lock:
            server._diagnostics[uri] = list(baseline_diags)
        return {"status": "ok", "baseline_captured": True, "baseline_count": len(baseline_diags)}

    async def _get_diagnostics(self, lang: str, workspace_root: str, file_path: str, use_delta: bool = False) -> Dict:
        server = await self.get_or_create(lang, workspace_root)
        if server is None:
            return {"error": f"Failed to start LSP for {lang}"}
        path = Path(file_path).resolve()
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            content = ""

        cfg = get_config(lang)
        if cfg and cfg.handler_module:
            handler = load_handler(lang)
            if handler and hasattr(handler, "after_initialize"):
                await handler.after_initialize(server)

        uri = path.as_uri()
        if uri not in server._open_documents:
            await server.did_open(path, content, version=1)
        wait_seconds = cfg.wait_seconds if cfg else 5.0
        await asyncio.sleep(wait_seconds)

        diags = server.get_diagnostics(path, timeout=5.0)
        debug_log(f"[_get_diagnostics] push diags: {len(diags)}")
        if not diags:
            try:
                pull_diags = await asyncio.wait_for(
                    server.request_diagnostics(path), timeout=10.0
                )
                if pull_diags:
                    diags = pull_diags
            except Exception as e:
                debug_log(f"[_get_diagnostics] pull failed: {e}")

        if use_delta:
            diags = self._store.delta(lang, workspace_root, file_path, diags)
            debug_log(f"[_get_diagnostics] delta diags: {len(diags)}")

        return {"diagnostics": self._store.compact(diags)}

    async def shutdown_all(self):
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            await server.shutdown()


# -- marker file creation -----------------------------------------------------

def _create_marker(marker: Path, marker_name: str) -> None:
    if marker_name == "pyproject.toml":
        marker.write_text("[build-system]\nrequires = [\"setuptools\"]\n")
    elif marker_name == "package.json":
        marker.write_text('{"name": "workspace"}\n')
    elif marker_name == "go.mod":
        marker.write_text("module workspace\n")
    elif marker_name == "Cargo.toml":
        marker.write_text("[package]\nname = \"workspace\"\nversion = \"0.1.0\"\n")
    elif marker_name.endswith(".csproj"):
        marker.write_text(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            '  <PropertyGroup>\n'
            '    <TargetFramework>net8.0</TargetFramework>\n'
            '  </PropertyGroup>\n'
            '</Project>\n'
        )
    elif marker_name.endswith(".sln"):
        logger.info("Skipping .sln marker creation — Roslyn requires valid .sln or none")
    else:
        marker.write_text("")
        logger.info("Created marker file: %s", marker)


# -- global singleton ---------------------------------------------------------

_lsp_manager: Optional[LSPServerManager] = None
_lsp_manager_lock = threading.Lock()


def get_lsp_manager() -> LSPServerManager:
    global _lsp_manager
    with _lsp_manager_lock:
        if _lsp_manager is None:
            debug_log("[get_lsp_manager] creating new LSPServerManager instance")
            _lsp_manager = LSPServerManager()
        return _lsp_manager
