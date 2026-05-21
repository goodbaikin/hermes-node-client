"""LSP RPC Server for hermes_node_client.

Provides language-server diagnostics via JSON-RPC over stdio,
exposed through the existing node_client HTTP/WebSocket protocol.

Each language gets a dedicated LSP subprocess.  Diagnostics are
pushed by the server, cached here, and returned on request.
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

# Debug log file for tracing LSP messages — controlled by HERMES_LSP_DEBUG env var.
# Set HERMES_LSP_DEBUG=1 to enable verbose JSON-RPC tracing.
_debug_log_path = Path.home() / ".hermes" / "logs" / "lsp" / "_debug_rpc.log"
_debug_enabled = os.environ.get("HERMES_LSP_DEBUG", "").strip() in ("1", "true", "yes")

def _debug_log(msg: str):
    if not _debug_enabled:
        return
    try:
        _debug_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_debug_log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Server registry — command lines and root markers per language
# ---------------------------------------------------------------------------
LSP_REGISTRY: Dict[str, Dict[str, Any]] = {
    "csharp": {
        "bin": "roslyn-language-server",
        "args": ["--stdio", "--autoLoadProjects"],
        "root_markers": [".sln", ".csproj"],
        "wait_seconds": 10.0,
        "skip_did_change": True,
    },
    "python": {
        "bin": "pyright-langserver",
        "args": ["--stdio"],
        "root_markers": ["pyproject.toml", "setup.py", "setup.cfg"],
        "wait_seconds": 3.0,
    },
    "typescript": {
        "bin": "typescript-language-server",
        "args": ["--stdio"],
        "root_markers": ["package.json", "tsconfig.json"],
        "wait_seconds": 3.0,
    },
    "javascript": {
        "bin": "typescript-language-server",
        "args": ["--stdio"],
        "root_markers": ["package.json"],
        "wait_seconds": 3.0,
    },
    "rust": {
        "bin": "rust-analyzer",
        "args": [],
        "root_markers": ["Cargo.toml"],
        "wait_seconds": 5.0,
    },
    "go": {
        "bin": "gopls",
        "args": [],
        "root_markers": ["go.mod"],
        "wait_seconds": 3.0,
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
        self._baseline: Dict[str, List[Dict]] = {}      # uri -> pre-edit diagnostics (for delta)
        self._lock = threading.Lock()
        self._reader_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._project_init_event = asyncio.Event()  # Wait for workspace/projectInitializationComplete
        self._open_documents: set[str] = set()  # Track opened documents to avoid didClose crashes

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

        # bin_path may be a list (e.g. ["cmd.exe", "/c", "..."]) for .cmd wrappers on Windows
        if isinstance(bin_path, list):
            args = bin_path + cfg["args"]
        else:
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
                "workspace": {
                    "workspaceFolders": True,
                    "configuration": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                },
                "textDocument": {
                    "publishDiagnostics": {"relatedInformation": True},
                    "synchronization": {"dynamicRegistration": True, "willSave": True, "willSaveWaitUntil": True, "didSave": True},
                },
            },
        })
        await self._send_notification("initialized", {})
        self._initialized = True
        logger.info("LSP initialized: %s", result is not None)

    async def did_open(self, file_path: Path, content: str, version: int = 1):
        if not self._initialized:
            return
        uri = file_path.as_uri()
        # If already open, close first to avoid "already open" errors
        if uri in self._open_documents:
            _debug_log(f"[did_open] {uri} already open, closing first")
            await self.did_close(file_path)
            await asyncio.sleep(0.5)
        await self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": self.lang,
                "version": version,
                "text": content,
            }
        })
        with self._lock:
            self._open_documents.add(uri)

    async def did_close(self, file_path: Path):
        if not self._initialized:
            return
        uri = file_path.as_uri()
        with self._lock:
            if uri not in self._open_documents:
                _debug_log(f"[did_close] skipping {uri} - not open")
                return
            self._open_documents.discard(uri)
        try:
            await self._send_notification("textDocument/didClose", {
                "textDocument": {"uri": uri},
            })
        except Exception as e:
            _debug_log(f"[did_close] error sending didClose for {uri}: {e}")

    async def did_change(self, file_path: Path, content: str, version: int = 2):
        """Send didChange notification.
        
        WARNING: Roslyn crashes on didChange with range=None (NullReferenceException
        in ProtocolConversions.RangeToLinePositionSpan). Avoid using didChange with
        Roslyn; use didClose+didOpen instead.
        """
        if not self._initialized:
            return
        uri = file_path.as_uri()
        # Send full document replacement with explicit range=None
        await self._send_notification("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"range": None, "text": content}],
        })

    async def did_save(self, file_path: Path):
        if not self._initialized:
            return
        uri = file_path.as_uri()
        await self._send_notification("textDocument/didSave", {
            "textDocument": {"uri": uri},
        })

    async def notify_file_changed(self, file_path: Path):
        """Notify server that a file has changed on disk (workspace/didChangeWatchedFiles)."""
        if not self._initialized:
            return
        uri = file_path.as_uri()
        await self._send_notification("workspace/didChangeWatchedFiles", {
            "changes": [{"uri": uri, "type": 2}]  # 2 = Changed
        })

    async def request_diagnostics(self, file_path: Path) -> List[Dict]:
        """Request diagnostics via LSP pull diagnostics (textDocument/diagnostic)."""
        if not self._initialized:
            return []
        uri = file_path.as_uri()
        result = await self._send_request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
            # Note: identifier is optional; Roslyn may use it to select diagnostic provider
        })
        items = []
        if result and "items" in result:
            items = result["items"]
        elif result and "relatedDocuments" in result:
            # Roslyn may return relatedDocuments with diagnostics
            docs = result.get("relatedDocuments", {})
            for doc_uri, doc_info in docs.items():
                if doc_uri == uri:
                    items = doc_info.get("items", [])
        # Cache the pull diagnostics so get_diagnostics can find them
        with self._lock:
            self._diagnostics[uri] = items
        return items

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

    def is_alive(self) -> bool:
        """Return True if the LSP subprocess is still running."""
        if self.process is None:
            _debug_log(f"[is_alive] process is None")
            return False
        retcode = self.process.returncode
        alive = retcode is None
        _debug_log(f"[is_alive] returncode={retcode}, alive={alive}")
        return alive

    def get_log_path(self) -> Optional[Path]:
        """Return path to stderr log file if available."""
        log_dir = Path.home() / ".hermes" / "logs" / "lsp"
        path = log_dir / f"{self.lang}_{self.workspace_root.name}.log"
        return path if path.exists() else None

    def snapshot_baseline(self, file_path: Path) -> None:
        """Capture current diagnostics as baseline for delta comparison."""
        uri = file_path.as_uri()
        with self._lock:
            self._baseline[uri] = list(self._diagnostics.get(uri, []))
        _debug_log(f"[baseline] captured {len(self._baseline.get(uri, []))} diags for {uri}")

    def get_delta_diagnostics(self, file_path: Path) -> List[Dict]:
        """Return diagnostics that are new compared to baseline."""
        uri = file_path.as_uri()
        with self._lock:
            current = self._diagnostics.get(uri, [])
            baseline = self._baseline.get(uri, [])
            _debug_log(f"[delta] current={len(current)}, baseline={len(baseline)}")
            if not baseline:
                return list(current)
            # Simple delta: filter out diagnostics with same code + line
            baseline_keys = set()
            for d in baseline:
                key = (d.get("code"), d.get("range", {}).get("start", {}).get("line"))
                baseline_keys.add(key)
            delta = []
            for d in current:
                key = (d.get("code"), d.get("range", {}).get("start", {}).get("line"))
                if key not in baseline_keys:
                    delta.append(d)
                    _debug_log(f"[delta] new: {key}")
                else:
                    _debug_log(f"[delta] skip: {key}")
            return delta

    def clear_baseline(self, file_path: Path) -> None:
        """Clear baseline for a file."""
        uri = file_path.as_uri()
        with self._lock:
            self._baseline.pop(uri, None)

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
        # 2. PATH
        found = shutil.which(name)
        if found:
            # On Windows, shutil.which may return a .cmd wrapper.
            # asyncio.create_subprocess_exec cannot run .cmd files directly.
            # Prefer the actual .exe if we can find it.
            if sys.platform == "win32" and found.lower().endswith(".cmd"):
                # Try to find the actual .exe in the same directory
                exe_candidate = Path(found).with_suffix(".exe")
                if exe_candidate.exists():
                    return str(exe_candidate)
                # For dotnet tools, look under .store
                if name == "roslyn-language-server":
                    dotnet_tools = Path.home() / ".dotnet" / "tools"
                    store_dir = dotnet_tools / ".store" / "roslyn-language-server"
                    if store_dir.exists():
                        for version_dir in store_dir.iterdir():
                            exe = version_dir / f"roslyn-language-server.win-x64" / version_dir.name / "tools" / "net10.0" / "win-x64" / "Microsoft.CodeAnalysis.LanguageServer.exe"
                            if exe.exists():
                                return str(exe)
                # Fall back to cmd.exe /c for .cmd wrappers
                return ["cmd.exe", "/c", found]
            return found
        # 3. common Windows locations
        if sys.platform == "win32":
            # For dotnet tools like roslyn-language-server, resolve .exe directly
            # (asyncio.create_subprocess_exec cannot run .cmd wrappers)
            if name == "roslyn-language-server":
                dotnet_tools = Path.home() / ".dotnet" / "tools"
                # Find the actual .exe under .store
                store_dir = dotnet_tools / ".store" / "roslyn-language-server"
                if store_dir.exists():
                    for version_dir in store_dir.iterdir():
                        exe = version_dir / f"roslyn-language-server.win-x64" / version_dir.name / "tools" / "net10.0" / "win-x64" / "Microsoft.CodeAnalysis.LanguageServer.exe"
                        if exe.exists():
                            return str(exe)
            
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
        _debug_log(f"SEND: {data[:800]}")
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
                    _debug_log(f"RECV: {json.dumps(msg, ensure_ascii=False)[:800]}")
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
        elif method == "workspace/configuration":
            # Respond to configuration requests with empty config
            params = msg.get("params", {})
            items = params.get("items", [])
            result = [{} for _ in items]
            asyncio.create_task(self._send_response(msg_id, result))
        elif method == "client/registerCapability":
            # Acknowledge capability registration
            asyncio.create_task(self._send_response(msg_id, None))
        elif method == "window/workDoneProgress/create":
            # Acknowledge progress creation
            asyncio.create_task(self._send_response(msg_id, None))
        elif method == "workspace/projectInitializationComplete":
            logger.info("Roslyn project initialization complete")
            self._project_init_event.set()

    async def wait_for_project_init(self, timeout: float = 30.0) -> bool:
        """Wait for workspace/projectInitializationComplete from Roslyn."""
        try:
            await asyncio.wait_for(self._project_init_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for project initialization")
            return False

    async def _send_response(self, msg_id: Any, result: Any):
        """Send a JSON-RPC response."""
        msg = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        await self._write(msg)

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

    # Class-level storage so instances share server registry across reloads
    _servers: Dict[str, Any] = {}
    _servers_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.Lock()
        # Per-server request serialization locks to ensure baseline/delta ordering
        self._server_locks: Dict[str, asyncio.Lock] = {}
        self._server_locks_lock = threading.Lock()
        # Persistent baselines: survive server crashes by storing at manager level
        # key = "{lang}:{workspace}:{file_uri}" -> List[Dict] (baseline diagnostics)
        self._baselines: Dict[str, List[Dict]] = {}

    def _baseline_key(self, lang: str, workspace_root: str, file_path: str) -> str:
        """Generate a persistent key for baseline storage."""
        root_path = Path(workspace_root).resolve().as_posix()
        uri = Path(file_path).resolve().as_uri()
        return f"{lang}:{root_path}:{uri}"

    def _store_baseline(self, lang: str, workspace_root: str, file_path: str, diags: List[Dict]) -> None:
        """Store baseline at manager level (survives server crashes)."""
        key = self._baseline_key(lang, workspace_root, file_path)
        with self._lock:
            self._baselines[key] = list(diags)
        _debug_log(f"[baseline_store] stored {len(diags)} diags for {key}")

    def _load_baseline(self, lang: str, workspace_root: str, file_path: str) -> List[Dict]:
        """Load baseline from manager level."""
        key = self._baseline_key(lang, workspace_root, file_path)
        with self._lock:
            diags = list(self._baselines.get(key, []))
        _debug_log(f"[baseline_load] loaded {len(diags)} diags for {key}")
        return diags

    def _clear_baseline(self, lang: str, workspace_root: str, file_path: str) -> None:
        """Clear baseline from manager level."""
        key = self._baseline_key(lang, workspace_root, file_path)
        with self._lock:
            self._baselines.pop(key, None)
        _debug_log(f"[baseline_clear] cleared {key}")

    def _get_server_lock(self, key: str) -> asyncio.Lock:
        with self._server_locks_lock:
            if key not in self._server_locks:
                self._server_locks[key] = asyncio.Lock()
            return self._server_locks[key]

    async def get_or_create(self, lang: str, workspace_root: str) -> Optional[LSPSubprocess]:
        # Normalize workspace_root for consistent key generation
        root_path = Path(workspace_root).resolve()
        key = f"{lang}:{root_path.as_posix()}"
        _debug_log(f"[get_or_create] key={key}")
        with self._lock:
            server = self._servers.get(key)
            _debug_log(f"[get_or_create] existing server={server is not None}, _servers keys={list(self._servers.keys())}")
            if server is not None and server.is_alive():
                _debug_log(f"[get_or_create] found existing healthy server for {key}")
                return server
            if server is not None and not server.is_alive():
                _debug_log(f"[get_or_create] removing dead server for {key}")
                # Clean up the dead server before removing
                try:
                    if server.process and server.process.returncode is None:
                        server.process.kill()
                except Exception:
                    pass
                self._servers.pop(key, None)

        _debug_log(f"[get_or_create] creating new server for {key}")
        server = LSPSubprocess(lang, root_path)
        ok = await server.start()
        if not ok:
            return None

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

        _debug_log(f"[handle_request] action={action}, lang={lang}, workspace_root={workspace_root}, file_path={file_path}, delta={use_delta}")

        # Normalize key for lock lookup (same logic as get_or_create)
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

    async def _lint_after_write(self, lang: str, workspace_root: str, file_path: str, content: str, use_delta: bool = False) -> Dict:
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

        # For Roslyn: wait for project initialization before sending didOpen
        if lang == "csharp" and cfg.get("bin") == "roslyn-language-server":
            logger.info("Waiting for Roslyn project initialization...")
            init_ok = await server.wait_for_project_init(timeout=25.0)
            if init_ok:
                logger.info("Roslyn project initialized, sending didOpen")
            else:
                logger.warning("Roslyn project init timeout, proceeding anyway")

        # Write content to actual file so Roslyn can detect changes
        try:
            path.write_text(content, encoding="utf-8")
            logger.info("Wrote %d bytes to %s", len(content), path)
        except Exception as e:
            logger.warning("Failed to write file: %s", e)

        # For Roslyn: avoid didClose/didOpen cycle which causes crashes.
        # Instead, rely on file system watcher + pull diagnostics.
        # Only send didOpen if the file hasn't been opened yet.
        if lang == "csharp":
            uri = path.as_uri()
            if uri not in server._open_documents:
                await server.did_open(path, content, version=1)
            # Notify that file changed on disk
            await server.notify_file_changed(path)
            # Also send didSave to trigger re-analysis
            await server.did_save(path)
        else:
            await server.did_close(path)
            await server.did_open(path, content, version=1)
            await server.did_save(path)

        # Wait for diagnostics to arrive — cap under node_invoke timeout (default 30s)
        wait_seconds = cfg.get("wait_seconds", 5.0)
        diag_timeout = 10.0
        logger.info("Waiting %.1fs for %s diagnostics...", wait_seconds, lang)
        await asyncio.sleep(wait_seconds)
        diags = server.get_diagnostics(path, timeout=diag_timeout)

        # Always try pull diagnostics for Roslyn to get accurate results
        if lang == "csharp":
            logger.info("Trying pull diagnostics for Roslyn...")
            try:
                pull_diags = await asyncio.wait_for(
                    server.request_diagnostics(path),
                    timeout=10.0
                )
                if pull_diags:
                    diags = pull_diags
                    logger.info("Pull diagnostics returned %d items", len(diags))
            except asyncio.TimeoutError:
                logger.warning("Pull diagnostics timed out")
            except Exception as e:
                logger.warning("Pull diagnostics failed: %s", e)
        elif not diags:
            logger.info("Push diagnostics empty, trying pull diagnostics...")
            try:
                pull_diags = await asyncio.wait_for(
                    server.request_diagnostics(path),
                    timeout=10.0
                )
                if pull_diags:
                    diags = pull_diags
                    logger.info("Pull diagnostics returned %d items", len(diags))
            except asyncio.TimeoutError:
                logger.warning("Pull diagnostics timed out")
            except Exception as e:
                logger.warning("Pull diagnostics failed: %s", e)

        # Apply delta filtering if requested
        if use_delta:
            # Use manager-level baseline (survives server crashes)
            baseline = self._load_baseline(lang, workspace_root, file_path)
            _debug_log(f"[delta] baseline={len(baseline)}, current={len(diags)}")
            _debug_log(f"[delta] baseline_keys={[(d.get('code'), d.get('range', {}).get('start', {}).get('line')) for d in baseline]}")
            _debug_log(f"[delta] current_keys={[(d.get('code'), d.get('range', {}).get('start', {}).get('line')) for d in diags]}")
            if baseline:
                baseline_keys = set()
                for d in baseline:
                    key = (d.get("code"), d.get("range", {}).get("start", {}).get("line"))
                    baseline_keys.add(key)
                delta = []
                for d in diags:
                    key = (d.get("code"), d.get("range", {}).get("start", {}).get("line"))
                    if key not in baseline_keys:
                        delta.append(d)
                        _debug_log(f"[delta] new: {key}")
                    else:
                        _debug_log(f"[delta] skip: {key}")
                diags = delta
            logger.info("Delta filtering: %d diagnostics after filtering", len(diags))

        result = {"diagnostics": diags}
        log_path = server.get_log_path()
        if log_path:
            result["lsp_log"] = str(log_path)
        return result

    async def _snapshot_baseline(self, lang: str, workspace_root: str, file_path: str, content: str = "") -> Dict:
        """Capture baseline diagnostics for delta comparison."""
        server = await self.get_or_create(lang, workspace_root)
        if server is None:
            return {"error": f"Failed to start LSP for {lang}"}
        path = Path(file_path).resolve()
        # Use provided content, or read from file if not provided
        if not content:
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                pass

        # For Roslyn: wait for project initialization before sending didOpen
        cfg = LSP_REGISTRY.get(lang)
        if lang == "csharp" and cfg and cfg.get("bin") == "roslyn-language-server":
            logger.info("Waiting for Roslyn project initialization...")
            init_ok = await server.wait_for_project_init(timeout=25.0)
            if init_ok:
                logger.info("Roslyn project initialized, sending didOpen")
            else:
                logger.warning("Roslyn project init timeout, proceeding anyway")

        # Ensure file is opened so server has diagnostics
        uri = path.as_uri()
        if uri not in server._open_documents:
            await server.did_open(path, content, version=1)
        wait_seconds = cfg.get("wait_seconds", 5.0) if cfg else 5.0
        await asyncio.sleep(wait_seconds)

        # Get diagnostics via pull request for reliable baseline capture
        baseline_diags = []
        try:
            baseline_diags = await asyncio.wait_for(
                server.request_diagnostics(path),
                timeout=10.0
            )
            logger.info("Baseline pull diagnostics: %d items", len(baseline_diags))
        except Exception:
            pass

        # Also wait briefly for any push diagnostics, but prefer pull result
        push_diags = server.get_diagnostics(path, timeout=2.0)
        if push_diags and not baseline_diags:
            baseline_diags = push_diags
            logger.info("Using push diagnostics for baseline: %d items", len(baseline_diags))

        # Store baseline at MANAGER level (survives server crashes)
        self._store_baseline(lang, workspace_root, file_path, baseline_diags)
        # Also store on server instance for backward compatibility
        uri = path.as_uri()
        with server._lock:
            server._baseline[uri] = list(baseline_diags)
            server._diagnostics[uri] = list(baseline_diags)
        _debug_log(f"[baseline] captured {len(baseline_diags)} diags for {uri}")
        _debug_log(f"[baseline] baseline_keys={[(d.get('code'), d.get('range', {}).get('start', {}).get('line')) for d in baseline_diags]}")

        return {"status": "ok", "baseline_captured": True, "baseline_count": len(baseline_diags)}

    async def _get_diagnostics(self, lang: str, workspace_root: str, file_path: str, use_delta: bool = False) -> Dict:
        server = await self.get_or_create(lang, workspace_root)
        if server is None:
            return {"error": f"Failed to start LSP for {lang}"}
        path = Path(file_path).resolve()
        # Ensure file is opened so server has diagnostics
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            content = ""

        # For Roslyn: wait for project initialization before sending didOpen
        cfg = LSP_REGISTRY.get(lang)
        if lang == "csharp" and cfg and cfg.get("bin") == "roslyn-language-server":
            logger.info("Waiting for Roslyn project initialization...")
            init_ok = await server.wait_for_project_init(timeout=25.0)
            if init_ok:
                logger.info("Roslyn project initialized, sending didOpen")
            else:
                logger.warning("Roslyn project init timeout, proceeding anyway")

        # Ensure file is opened so server has diagnostics
        uri = path.as_uri()
        if uri not in server._open_documents:
            await server.did_open(path, content, version=1)
        wait_seconds = cfg.get("wait_seconds", 5.0) if cfg else 5.0
        await asyncio.sleep(wait_seconds)
        # Try push diagnostics first, then pull
        diags = server.get_diagnostics(path, timeout=5.0)
        _debug_log(f"[_get_diagnostics] push diags: {len(diags)}")
        if not diags:
            try:
                _debug_log(f"[_get_diagnostics] trying pull diagnostics...")
                pull_diags = await asyncio.wait_for(
                    server.request_diagnostics(path),
                    timeout=10.0
                )
                _debug_log(f"[_get_diagnostics] pull diags: {len(pull_diags) if pull_diags else 0}")
                if pull_diags:
                    diags = pull_diags
            except Exception as e:
                _debug_log(f"[_get_diagnostics] pull failed: {e}")
                pass
        if use_delta:
            # Use manager-level baseline (survives server crashes)
            baseline = self._load_baseline(lang, workspace_root, file_path)
            current_diags = diags
            if baseline:
                baseline_keys = set()
                for d in baseline:
                    key = (d.get("code"), d.get("range", {}).get("start", {}).get("line"))
                    baseline_keys.add(key)
                delta = []
                for d in current_diags:
                    key = (d.get("code"), d.get("range", {}).get("start", {}).get("line"))
                    if key not in baseline_keys:
                        delta.append(d)
                diags = delta
            _debug_log(f"[_get_diagnostics] delta diags: {len(diags)}")
        return {"diagnostics": [_compact_diag(d) for d in diags]}

def _compact_diag(d: Dict) -> Dict:
    """Strip large fields from diagnostic to keep WebSocket message small."""
    return {
        "code": d.get("code"),
        "message": d.get("message", ""),
        "severity": d.get("severity", 1),
        "range": d.get("range", {}),
    }

    async def shutdown_all(self):
        """Shutdown all managed LSP servers."""
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            await server.shutdown()


# Global singleton manager instance
# Global singleton manager instance
_lsp_manager: Optional[LSPServerManager] = None
_lsp_manager_lock = threading.Lock()
_lsp_manager_file = Path.home() / ".hermes" / "lsp_manager.pkl"

def _save_lsp_manager(mgr: LSPServerManager):
    """Save LSP manager state to file."""
    try:
        import pickle
        _lsp_manager_file.parent.mkdir(parents=True, exist_ok=True)
        with open(_lsp_manager_file, "wb") as f:
            pickle.dump({"servers": list(mgr._servers.keys())}, f)
    except Exception:
        pass

def _load_lsp_manager() -> Optional[LSPServerManager]:
    """Load LSP manager state from file."""
    try:
        import pickle
        if _lsp_manager_file.exists():
            with open(_lsp_manager_file, "rb") as f:
                data = pickle.load(f)
                # Return a manager with the same servers dict
                mgr = LSPServerManager()
                return mgr
    except Exception:
        pass
    return None

def get_lsp_manager() -> LSPServerManager:
    """Return the global LSP server manager singleton."""
    global _lsp_manager
    with _lsp_manager_lock:
        if _lsp_manager is None:
            _debug_log("[get_lsp_manager] creating new LSPServerManager instance")
            _lsp_manager = LSPServerManager()
        return _lsp_manager
