"""LSP subprocess wrapper — JSON-RPC over stdio."""
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

from .registry import get_config
from .utils import debug_log

logger = logging.getLogger(__name__)


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
        self._project_init_event = asyncio.Event()
        self._open_documents: set[str] = set()

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> bool:
        cfg = get_config(self.lang)
        if cfg is None:
            logger.error("No LSP config for language: %s", self.lang)
            return False

        bin_path = self._resolve_bin(cfg.bin)
        if bin_path is None:
            logger.error("LSP binary not found: %s", cfg.bin)
            return False

        if isinstance(bin_path, list):
            args = bin_path + cfg.args
        else:
            args = [bin_path] + cfg.args
        logger.info("Starting LSP: %s", " ".join(str(a) for a in args))

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

    # -- standard LSP methods -------------------------------------------------

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
                    "synchronization": {
                        "dynamicRegistration": True,
                        "willSave": True,
                        "willSaveWaitUntil": True,
                        "didSave": True,
                    },
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
        if uri in self._open_documents:
            debug_log(f"[did_open] {uri} already open, closing first")
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
                debug_log(f"[did_close] skipping {uri} - not open")
                return
            self._open_documents.discard(uri)
        try:
            await self._send_notification("textDocument/didClose", {
                "textDocument": {"uri": uri},
            })
        except Exception as e:
            debug_log(f"[did_close] error sending didClose for {uri}: {e}")

    async def did_change(self, file_path: Path, content: str, version: int = 2):
        if not self._initialized:
            return
        uri = file_path.as_uri()
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
        if not self._initialized:
            return
        uri = file_path.as_uri()
        await self._send_notification("workspace/didChangeWatchedFiles", {
            "changes": [{"uri": uri, "type": 2}]  # 2 = Changed
        })

    async def request_diagnostics(self, file_path: Path) -> List[Dict]:
        if not self._initialized:
            return []
        uri = file_path.as_uri()
        result = await self._send_request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })
        items = []
        if result and "items" in result:
            items = result["items"]
        elif result and "relatedDocuments" in result:
            docs = result.get("relatedDocuments", {})
            for doc_uri, doc_info in docs.items():
                if doc_uri == uri:
                    items = doc_info.get("items", [])
        with self._lock:
            self._diagnostics[uri] = items
        return items

    def get_diagnostics(self, file_path: Path, timeout: float = 5.0) -> List[Dict]:
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
        if self.process is None:
            return False
        return self.process.returncode is None

    def get_log_path(self) -> Optional[Path]:
        log_dir = Path.home() / ".hermes" / "logs" / "lsp"
        path = log_dir / f"{self.lang}_{self.workspace_root.name}.log"
        return path if path.exists() else None

    async def wait_for_project_init(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._project_init_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for project initialization")
            return False

    # -- binary resolution ----------------------------------------------------

    def _resolve_bin(self, name: str) -> Optional[str]:
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

        staged = Path.home() / ".hermes" / "lsp" / "bin" / name
        if staged.exists():
            return str(staged)

        found = shutil.which(name)
        if found:
            if sys.platform == "win32" and found.lower().endswith(".cmd"):
                exe_candidate = Path(found).with_suffix(".exe")
                if exe_candidate.exists():
                    return str(exe_candidate)
                if name == "roslyn-language-server":
                    dotnet_tools = Path.home() / ".dotnet" / "tools"
                    store_dir = dotnet_tools / ".store" / "roslyn-language-server"
                    if store_dir.exists():
                        for version_dir in store_dir.iterdir():
                            exe = version_dir / f"roslyn-language-server.win-x64" / version_dir.name / "tools" / "net10.0" / "win-x64" / "Microsoft.CodeAnalysis.LanguageServer.exe"
                            if exe.exists():
                                return str(exe)
                return ["cmd.exe", "/c", found]
            # WindowsApps apps (pwsh, python3, etc.) need special handling
            if sys.platform == "win32" and "windowsapps" in found.lower():
                # Use where.exe to get the actual executable path
                import subprocess
                try:
                    where_result = subprocess.run(
                        ["where.exe", name],
                        capture_output=True, text=True, timeout=5
                    )
                    if where_result.returncode == 0:
                        # where.exe returns all matches, take the first non-WindowsApps one
                        for line in where_result.stdout.strip().splitlines():
                            line = line.strip()
                            if line and "windowsapps" not in line.lower():
                                return line
                        # Fall back to first match
                        first = where_result.stdout.strip().splitlines()[0].strip()
                        if first:
                            return first
                except Exception:
                    pass
                # Last resort: powershell -Command to launch it
                return ["powershell.exe", "-Command", name]
            return found

        if sys.platform == "win32":
            if name == "roslyn-language-server":
                dotnet_tools = Path.home() / ".dotnet" / "tools"
                store_dir = dotnet_tools / ".store" / "roslyn-language-server"
                if store_dir.exists():
                    for version_dir in store_dir.iterdir():
                        exe = version_dir / f"roslyn-language-server.win-x64" / version_dir.name / "tools" / "net10.0" / "win-x64" / "Microsoft.CodeAnalysis.LanguageServer.exe"
                        if exe.exists():
                            return str(exe)
            # pwsh (PowerShell 7) — prefer Program Files over WindowsApps
            if name == "pwsh":
                for pwsh_path in [
                    Path("C:/Program Files/PowerShell/7/pwsh.exe"),
                    Path("C:/Program Files/PowerShell/6/pwsh.exe"),
                ]:
                    if pwsh_path.exists():
                        return str(pwsh_path)
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

    # -- JSON-RPC I/O ---------------------------------------------------------

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

    async def _send_response(self, msg_id: Any, result: Any):
        await self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def _write(self, msg: Dict):
        data = json.dumps(msg, ensure_ascii=False)
        header = f"Content-Length: {len(data.encode('utf-8'))}\r\n\r\n"
        payload = (header + data).encode("utf-8")
        debug_log(f"SEND: {data[:800]}")
        if self.process and self.process.stdin:
            self.process.stdin.write(payload)
            await self.process.stdin.drain()

    # -- read loop ------------------------------------------------------------

    async def _read_loop(self):
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
                    debug_log(f"RECV: {json.dumps(msg, ensure_ascii=False)[:800]}")
                    self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("LSP read error: %s", exc)
                break

    def _parse_message(self, buf: bytes) -> tuple:
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
            logger.info("LSP [%s]: %s", log_level, message)
        elif method == "workspace/configuration":
            params = msg.get("params", {})
            items = params.get("items", [])
            result = [{} for _ in items]
            asyncio.create_task(self._send_response(msg_id, result))
        elif method == "client/registerCapability":
            asyncio.create_task(self._send_response(msg_id, None))
        elif method == "window/workDoneProgress/create":
            asyncio.create_task(self._send_response(msg_id, None))
        elif method == "workspace/projectInitializationComplete":
            logger.info("Project initialization complete")
            self._project_init_event.set()
