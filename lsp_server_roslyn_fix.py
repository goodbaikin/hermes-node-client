"""LSP RPC Server for hermes_node_client — Roslyn-compatible version.

Fixes:
1. Resolve .exe directly for dotnet tools (avoid .cmd wrappers)
2. Shorter wait times for Roslyn (5-10s vs 60s)
3. Skip didChange for Roslyn (crashes without range)
4. Better server cleanup on timeout
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server registry — command lines and root markers per language
# ---------------------------------------------------------------------------
LSP_REGISTRY: Dict[str, Dict[str, Any]] = {
    "csharp": {
        "bin": "roslyn-language-server",
        "args": ["--stdio", "--autoLoadProjects", "--logLevel", "Information"],
        "root_markers": [".sln", ".csproj"],
        "wait_seconds": 10.0,
        "skip_did_change": True,
    },
    "python": {
        "bin": "pyright-langserver",
        "args": ["--stdio"],
        "root_markers": ["pyproject.toml", "setup.py", "setup.cfg"],
        "wait_seconds": 3.0,
        "skip_did_change": False,
    },
    "typescript": {
        "bin": "typescript-language-server",
        "args": ["--stdio"],
        "root_markers": ["package.json", "tsconfig.json"],
        "wait_seconds": 3.0,
        "skip_did_change": False,
    },
    "javascript": {
        "bin": "typescript-language-server",
        "args": ["--stdio"],
        "root_markers": ["package.json"],
        "wait_seconds": 3.0,
        "skip_did_change": False,
    },
    "rust": {
        "bin": "rust-analyzer",
        "args": [],
        "root_markers": ["Cargo.toml"],
        "wait_seconds": 5.0,
        "skip_did_change": False,
    },
    "go": {
        "bin": "gopls",
        "args": [],
        "root_markers": ["go.mod"],
        "wait_seconds": 3.0,
        "skip_did_change": False,
    },
}


class LSPSubprocess:
    """Wraps one language-server subprocess and its JSON-RPC channel."""

    def __init__(self, lang: str, workspace_root: Path):
        self.lang = lang
        self.workspace_root = workspace_root.resolve()
        self.process: Optional[asyncio.subprocess.Process] = None
        self._seq = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._diagnostics: Dict[str, List[Dict]] = {}  # uri -> diagnostics
        self._lock = threading.Lock()
        self._reader_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._server_specific = LSP_REGISTRY.get(lang, {})

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> bool:
        cfg = LSP_REGISTRY.get(self.lang)
        if cfg is None:
            logger.error("No LSP config for language: %s", self.lang)
            return False

        bin_path = self._resolve_bin(cfg["bin"])
        if bin_path is None:
            logger.error("LSP binary not found: %s", cfg["bin"])
            return False

        args = [bin_path] + cfg["args"]
        logger.info("Starting LSP: %s", " ".join(str(a) for a in args))

        # Redirect stderr to log file for debugging
        log_dir = Path.home() / ".hermes" / "logs" / "lsp"
        log_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = log_dir / f"{self.lang}_{self.workspace_root.name}.log"
        try:
            stderr_file = open(stderr_path, "ab")
        except OSError:
            stderr_file = None

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_file or asyncio.subprocess.PIPE,
                cwd=self.workspace_root,
            )
        except Exception as exc:
            logger.error("Failed to start LSP: %s", exc)
            if stderr_file:
                stderr_file.close()
            return False

        self._reader_task = asyncio.create_task(self._read_loop())
        await self._initialize()
        return True

    async def shutdown(self):
        if self.process is None:
            return
        try:
            await self._send_request("shutdown", {})
            await self._send_notification("exit", {})
            await asyncio.wait_for(self.process.wait(), timeout=5.0)
        except Exception:
            self.process.kill()
        self.process = None
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    # -- Standard LSP JSON-RPC ----------------------------------------------

    async def _initialize(self):
        root_uri = self.workspace_root.as_uri()
        result = await self._send_request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.workspace_root.name}],
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {"relatedInformation": True},
                }
            },
        })
        await self._send_notification("initialized", {})
        self._initialized = True
        logger.info("LSP initialized: %s", result is not None)

    async def did_open(self, file_path: Path, content: str, version: int = 1):
        if not self._initialized:
            return
        uri = file_path.as_uri()
        await self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": self.lang,
                "version": version,
                "text": content,
            }
        })

    async def did_change(self, file_path: Path, content: str, version: int = 2):
        if not self._initialized:
            return
        if self._server_specific.get("skip_did_change"):
            logger.debug("Skipping didChange for %s (server-specific quirk)", self.lang)
            return
        uri = file_path.as_uri()
        await self._send_notification("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": content}],
        })

    async def did_save(self, file_path: Path):
        if not self._initialized:
            return
        uri = file_path.as_uri()
        await self._send_notification("textDocument/didSave", {
            "textDocument": {"uri": uri},
        })

    async def request_diagnostics(self, file_path: Path) -> List[Dict]:
        """Explicitly request diagnostics via workspace/executeCommand or similar."""
        return []

    def get_diagnostics(self, file_path: Path, timeout: float = 5.0) -> List[Dict]:
        """Synchronous wrapper — polls cache until timeout."""
        uri = file_path.as_uri()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                diags = self._diagnostics.get(uri, [])
                if diags:
                    return diags
            time.sleep(0.1)
        with self._lock:
            return self._diagnostics.get(uri, [])

    def get_log_path(self) -> Optional[Path]:
        """Return path to stderr log file if available."""
        log_dir = Path.home() / ".hermes" / "logs" / "lsp"
        path = log_dir / f"{self.lang}_{self.workspace_root.name}.log"
        return path if path.exists() else None

    # -- internals ----------------------------------------------------------

    def _resolve_bin(self, name: str) -> Optional[str]:
        """Resolve LSP binary path with support for .env overrides."""
        # 0. Check HERMES_LSP_PATH environment variable (from .env or shell)
        env_path = os.environ.get("HERMES_LSP_PATH", "")
        if env_path:
            for base in env_path.split(os.pathsep):
                if not base:
                    continue
                candidate = Path(base) / name
                if candidate.exists():
                    return str(candidate)
                candidate = candidate.with_suffix(".exe")
                if candidate.exists():
                    return str(candidate)

        # 1. staged hermes lsp bin dir
        staged = Path.home() / ".hermes" / "lsp" / "bin" / name
        if staged.exists():
            return str(staged)

        # 2. For dotnet tools on Windows, resolve .exe directly BEFORE shutil.which
        #    (asyncio.create_subprocess_exec cannot run .cmd wrappers)
        if sys.platform == "win32" and name in ("roslyn-language-server", "csharp-ls"):
            dotnet_tools = Path.home() / ".dotnet" / "tools"
            store_dir = dotnet_tools / ".store" / name
            if store_dir.exists():
                for version_dir in store_dir.iterdir():
                    if name == "roslyn-language-server":
                        exe = (version_dir / f"roslyn-language-server.win-x64" / version_dir.name
                               / "tools" / "net10.0" / "win-x64" / "Microsoft.CodeAnalysis.LanguageServer.exe")
                    else:
                        exe = version_dir / name / version_dir.name / "tools" / "net9.0" / "any" / "csharp-ls.dll"
                    if exe.exists():
                        return str(exe)
            # Also check direct .exe in tools dir
            direct_exe = dotnet_tools / "Microsoft.CodeAnalysis.LanguageServer.exe"
            if direct_exe.exists():
                return str(direct_exe)

        # 3. PATH — but reject .cmd wrappers on Windows
        found = shutil.which(name)
        if found:
            # On Windows, .cmd wrappers don't work with asyncio.create_subprocess_exec
            if sys.platform == "win32" and found.lower().endswith(".cmd"):
                logger.warning("Rejecting .cmd wrapper: %s", found)
                found = None
            else:
                return found

        # 4. common Windows locations
        if sys.platform == "win32":
            for base in [
                Path(os.environ.get("LOCALAPPDATA", "")) / "npm",
                Path(os.environ.get("APPDATA", "")) / "npm",
                Path.home() / "scoop" / "shims",
                Path.home() / ".dotnet" / "tools",
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "OmniSharp",
                Path(os.environ.get("LOCALAPPDATA", "")) / "OmniSharp",
            ]:
                candidate = base / name
                if candidate.exists():
                    return str(candidate)
                candidate = candidate.with_suffix(".exe")
                if candidate.exists():
                    return str(candidate)
        return None

    async def _send_request(self, method: str, params: Any) -> Any:
        self._seq += 1
        msg_id = self._seq
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        await self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=60.0)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            return None

    async def _send_notification(self, method: str, params: Any):
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, msg: Dict):
        data = json.dumps(msg, ensure_ascii=False)
        header = f"Content-Length: {len(data.encode('utf-8'))}\r\n\r\n"
        payload = (header + data).encode("utf-8")
        if self.process and self.process.stdin:
            self.process.stdin.write(payload)
            await self.process.stdin.drain()

    # -- read loop ------------------------------------------------------------

    async def _read_loop(self):
        """Read JSON-RPC messages from stdout."""
        if self.process is None or self.process.stdout is None:
            return
        buf = b""
        while True:
            try:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    msg, consumed = self._parse_message(buf)
                    if msg is None:
                        break
                    buf = buf[consumed:]
                    self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("LSP read error: %s", exc)
                break

    def _parse_message(self, buf: bytes) -> tuple:
        """Parse one JSON-RPC message from buffer. Returns (msg, consumed_bytes)."""
        # Standard LSP protocol (Content-Length header)
        try:
            header_end = buf.index(b"\r\n\r\n")
            header = buf[:header_end].decode("ascii", errors="replace")
            body_start = header_end + 4
            length = None
            for line in header.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
                    break
            if length is not None:
                if len(buf) < body_start + length:
                    return None, 0
                body = buf[body_start:body_start + length]
                msg = json.loads(body.decode("utf-8"))
                return msg, body_start + length
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            pass

        return None, 0

    def _handle_message(self, msg: Dict):
        # Standard LSP JSON-RPC
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                future.set_result(msg.get("result"))
            return

        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            uri = params.get("uri", "")
            diags = params.get("diagnostics", [])
            with self._lock:
                self._diagnostics[uri] = diags
            logger.debug("Diagnostics for %s: %d items", uri, len(diags))
        elif method == "window/logMessage":
            params = msg.get("params", {})
            msg_type = params.get("type", 0)
            message = params.get("message", "")
            log_level = {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "DEBUG"}.get(msg_type, "UNKNOWN")
            logger.info("Roslyn [%s]: %s", log_level, message)

    @staticmethod
    def _path_to_uri(path: str) -> str:
        """Convert a file path to URI."""
        if path.startswith("file://"):
            return path
        if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
            return "file://" + path.replace("\\", "/")
        return path


class LSPServerManager:
    """Manages multiple LSP subprocesses keyed by (language, workspace_root)."""

    def __init__(self):
        self._servers: Dict[str, LSPSubprocess] = {}
        self._lock = threading.Lock()

    async def get_or_create(self, lang: str, workspace_root: str) -> Optional[LSPSubprocess]:
        key = f"{lang}:{workspace_root}"
        with self._lock:
            server = self._servers.get(key)
            if server is not None:
                return server

        root_path = Path(workspace_root).resolve()
        server = LSPSubprocess(lang, root_path)
        ok = await server.start()
        if not ok:
            return None

        with self._lock:
            self._servers[key] = server
        return server

    async def handle_request(self, request: Dict) -> Dict:
        """Entry point called by node_client HTTP handler."""
        action = request.get("action")
        lang = request.get("language")
        workspace_root = request.get("workspace_root", str(Path.cwd()))
        file_path = request.get("file_path")
        content = request.get("content", "")

        if action == "lint_after_write":
            return await self._lint_after_write(lang, workspace_root, file_path, content)

        if action == "get_diagnostics":
            return await self._get_diagnostics(lang, workspace_root, file_path)

        if action == "shutdown":
            await self.shutdown_all()
            return {"status": "ok"}

        return {"error": f"unknown action: {action}"}

    async def _lint_after_write(self, lang: str, workspace_root: str, file_path: str, content: str) -> Dict:
        root = Path(workspace_root).resolve()

        # Ensure workspace has a marker file so LSP servers recognize it
        cfg = LSP_REGISTRY.get(lang)
        if cfg and "root_markers" in cfg:
            markers = cfg["root_markers"]
            has_marker = any((root / m).exists() for m in markers)
            if not has_marker and markers:
                # Create the first marker as a minimal stub
                marker = root / markers[0]
                if not marker.exists():
                    if markers[0] == "pyproject.toml":
                        marker.write_text("[build-system]\nrequires = [\"setuptools\"]\n")
                    elif markers[0] == "package.json":
                        marker.write_text('{"name": "workspace"}\n')
                    elif markers[0] == "go.mod":
                        marker.write_text("module workspace\n")
                    elif markers[0] == "Cargo.toml":
                        marker.write_text("[package]\nname = \"workspace\"\nversion = \"0.1.0\"\n")
                    elif markers[0].endswith(".csproj"):
                        marker.write_text(
                            '<Project Sdk="Microsoft.NET.Sdk">\n'
                            '  <PropertyGroup>\n'
                            '    <TargetFramework>net8.0</TargetFramework>\n'
                            '  </PropertyGroup>\n'
                            '</Project>\n'
                        )
                    elif markers[0].endswith(".sln"):
                        logger.info("Skipping .sln marker creation — Roslyn requires valid .sln or none")
                    else:
                        marker.write_text("")
                        logger.info("Created marker file: %s", marker)

        server = await self.get_or_create(lang, workspace_root)
        if server is None:
            return {"error": f"Failed to start LSP for {lang}"}

        path = Path(file_path).resolve()
        await server.did_open(path, content, version=1)

        # Skip didChange for Roslyn (crashes without range field)
        if not cfg.get("skip_did_change"):
            await server.did_change(path, content, version=2)

        await server.did_save(path)

        # Wait for diagnostics to arrive — use server-specific wait time
        wait_seconds = cfg.get("wait_seconds", 5.0) if cfg else 5.0
        diag_timeout = 10.0
        logger.info("Waiting %.1fs for %s diagnostics...", wait_seconds, lang)
        await asyncio.sleep(wait_seconds)
        diags = server.get_diagnostics(path, timeout=diag_timeout)

        result = {"diagnostics": diags}
        log_path = server.get_log_path()
        if log_path:
            result["lsp_log"] = str(log_path)
        return result

    async def _get_diagnostics(self, lang: str, workspace_root: str, file_path: str) -> Dict:
        server = await self.get_or_create(lang, workspace_root)
        if server is None:
            return {"error": f"Failed to start LSP for {lang}"}
        path = Path(file_path).resolve()
        diags = server.get_diagnostics(path, timeout=5.0)
        return {"diagnostics": diags}

    async def shutdown_all(self):
        """Shutdown all managed LSP servers."""
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            await server.shutdown()


# Global singleton manager instance
_lsp_manager: Optional[LSPServerManager] = None


def get_lsp_manager() -> LSPServerManager:
    """Return the global LSP server manager singleton."""
    global _lsp_manager
    if _lsp_manager is None:
        _lsp_manager = LSPServerManager()
    return _lsp_manager
