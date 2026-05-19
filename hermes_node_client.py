#!/usr/bin/env python3
"""
Hermes Remote Node Client for Windows
=====================================
OpenClaw-style WebSocket node that runs on a Windows development machine,
exposing local tool execution (PowerShell, MSBuild, signtool, file I/O)
to a remote Hermes Gateway without SSH escaping/encoding issues.

Features:
    - Auto-reconnect with exponential backoff
    - Persistent background execution (Windows Service compatible)
    - BOM/CRLF aware file I/O for Japanese text
    - Heartbeat/ping support

Usage (foreground):
    python hermes_node_client.py --gateway ws://hermes-gateway:8642/ws \
        --token your-secret-token --node-id dev-win01

    python hermes_node_client.py --api-server ws://api-server:8642/ws \
        --token your-secret-token --node-id dev-win01

Usage (install as Windows Service via NSSM):
    nssm install HermesNode "C:\\Path\\To\\python.exe" \
        "C:\\Path\\To\\hermes_node_client.py --api-server ws://... --token ... --node-id ..."

Dependencies:
    pip install websockets

Security:
    - Use wss:// (TLS) in production
    - Keep NODE_TOKEN in environment variables, never hardcode
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Fix Windows console encoding to UTF-8 before any I/O happens
if sys.platform == "win32":
    import io
    # Force UTF-8 for stdout/stderr to avoid cp932 UnicodeEncodeError
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Suppress websockets library debug logs BEFORE importing websockets
# to prevent cp932 encoding errors when binary frame data is logged
# on Japanese Windows systems
logging.getLogger("websockets").setLevel(logging.WARNING)

import websockets

# ---------------------------------------------------------------------------
# Load .env file from the same directory as this script
# ---------------------------------------------------------------------------
def _load_dotenv():
    """Load environment variables from .env file next to this script."""
    script_dir = Path(__file__).resolve().parent
    env_file = script_dir / ".env"
    if not env_file.exists():
        return
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass

_load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup (Windows Event Log friendly)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
import logging, sys
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)
# Cross-platform log path
if sys.platform == "win32":
    log_path = os.path.join(os.environ.get("TEMP", "C:\\Windows\\Temp"), "node_client_debug.log")
else:
    log_path = "/tmp/node_client_debug.log"
fh = logging.FileHandler(log_path, mode='w')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
sh = logging.StreamHandler(sys.stdout)
sh.setLevel(logging.INFO)
sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.root.addHandler(fh)
logging.root.addHandler(sh)
logging.root.setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (override via env vars or CLI)
# ---------------------------------------------------------------------------
NODE_ID = os.environ.get("HERMES_NODE_ID", "dev-win01")
GATEWAY_URL = os.environ.get("HERMES_GATEWAY_URL", "ws://localhost:8642/ws")
API_SERVER_URL = os.environ.get("HERMES_API_SERVER_URL", "")
NODE_TOKEN = os.environ.get("HERMES_NODE_TOKEN", "dev-token-change-me")

RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_BACKOFF_MULTIPLIER = 2.0

COMMANDS = {
    "terminal.exec",
    "terminal.stream",
    "file.read",
    "file.write",
    "file.delete",
    "file.list",
    "search.content",
    "search.files",
    "msbuild",
    "signtool",
    "node.restart",
    "lsp",
}

DEFAULT_TIMEOUT_SEC = 30
MAX_TIMEOUT_SEC = 600


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_terminal_exec(params: dict[str, Any]) -> dict[str, Any]:
    """Execute PowerShell locally. No SSH escaping hell."""
    cmd = params["cmd"]
    cwd = params.get("cwd")
    timeout_sec = min(
        params.get("timeoutMs", DEFAULT_TIMEOUT_SEC * 1000) / 1000, MAX_TIMEOUT_SEC
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # On Windows: use PowerShell with UTF-8 encoding, bypassing cmd.exe AutoRun
    # Skip wrapping if the command already invokes PowerShell/pwsh directly
    if sys.platform == "win32" and not cmd.strip().lower().startswith(("powershell", "pwsh")):
        # Use -Command instead of -EncodedCommand to preserve stderr correctly.
        # Escape double quotes in the user command so they survive the outer
        # argument list passed to create_subprocess_exec.
        escaped_cmd = cmd.replace('"', '\\"')
        ps_script = (
            '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
            '$OutputEncoding = [System.Text.Encoding]::UTF8; '
            '$ErrorView = \"NormalView\"; '
            f'{escaped_cmd}'
        )
        args = [
            _resolve_command("powershell"), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", ps_script,
        ]
    else:
        args = [_resolve_command("bash"), "-c", cmd]

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout_sec}s",
            "exitCode": -1,
        }

    return {
        "stdout": _normalize_crlf(stdout.decode("utf-8", errors="replace")),
        "stderr": _normalize_crlf(_strip_clixml(stderr.decode("utf-8", errors="replace"))),
        "exitCode": proc.returncode,
    }


def _strip_clixml(text: str) -> str:
    """Remove PowerShell CLIXML serialization from stderr output.

    PowerShell serializes ErrorRecord objects to XML when stderr is redirected.
    This strips the '#< CLIXML' header and the entire XML block that follows.
    """
    import re
    # Match '#< CLIXML' followed by an XML block (Objs element)
    pattern = re.compile(r'#<\s*CLIXML\s*<Objs[^>]*>.*?</Objs>', re.DOTALL | re.IGNORECASE)
    cleaned = pattern.sub('', text)
    # Also strip any remaining CLIXML header if XML parsing failed
    cleaned = cleaned.replace('#< CLIXML', '').replace('#<CLIXML', '')
    return cleaned.strip()


def _normalize_crlf(text: str) -> str:
    """Normalize CRLF to LF for consistent cross-platform text handling."""
    return text.replace('\r\n', '\n') if '\r\n' in text else text


def _resolve_command(name: str) -> str:
    """Resolve a command name to an absolute path via shutil.which().

    On Windows, commands like ``powershell`` may not be on PATH in some
    configurations (e.g. restricted shells, custom installs).  Using
    ``shutil.which()`` returns the fully-qualified path when found,
    which is more robust than relying on bare-name resolution.

    Falls back to the bare name if not found on PATH — the subsequent
    Popen will raise FileNotFoundError with a readable error.
    """
    resolved = shutil.which(name)
    return resolved if resolved else name


def _pid_exists(pid: int) -> bool:
    """Cross-platform "is this PID alive" check that does NOT kill the target.

    CRITICAL on Windows: Python's ``os.kill(pid, 0)`` is NOT a no-op like it
    is on POSIX. CPython's Windows implementation treats ``sig=0`` as
    ``CTRL_C_EVENT`` and routes it through ``GenerateConsoleCtrlEvent(0, pid)``
    — which sends a Ctrl+C to the entire console process group containing the
    target PID, not just the PID itself.  Any caller that wanted to "check if
    this PID is alive" via ``os.kill(pid, 0)`` on Windows was silently killing
    that process (and often unrelated processes).  Long-standing Python quirk;
    see bpo-14484.

    Fix: use the Win32 ``OpenProcess`` / ``WaitForSingleObject`` pair on
    Windows to check existence without any signal path; use the POSIX
    ``os.kill(pid, 0)`` idiom on POSIX where it actually is a no-op.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # Pin return types — default ctypes restype is c_int (signed),
            # which mangles WAIT_* DWORD return codes into negative numbers.
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.restype = ctypes.c_uint
            kernel32.GetLastError.restype = ctypes.c_uint
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x100000  # required for WaitForSingleObject
            WAIT_TIMEOUT = 0x00000102
            ERROR_INVALID_PARAMETER = 87
            ERROR_ACCESS_DENIED = 5
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid)
            )
            if not handle:
                err = kernel32.GetLastError()
                if err == ERROR_INVALID_PARAMETER:
                    return False  # PID definitely gone
                if err == ERROR_ACCESS_DENIED:
                    return True   # Exists but owned by another user/session
                return False      # Conservative default for unknown errors
            try:
                wait_result = kernel32.WaitForSingleObject(handle, 0)
                # WAIT_TIMEOUT = still running; anything else = gone.
                return wait_result == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return False
    else:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we can't signal it — still alive.
            return True
        except OSError:
            return False


async def handle_file_read(params: dict[str, Any]) -> dict[str, Any]:
    """Read a file locally. Supports binary (base64) or text (UTF-8).
    
    Args:
        path: File path to read
        encoding: Text encoding (default: binary/base64)
        offset: Line number to start from (1-based, for text mode)
        limit: Maximum lines to return (for text mode)
    """
    path = Path(params["path"])
    encoding = params.get("encoding")
    offset = params.get("offset", 1)
    limit = params.get("limit")

    if path.is_dir():
        return {"error": f"Path is a directory: {path}"}
    if not path.exists():
        return {"error": f"File not found: {path}"}

    if encoding:
        text = path.read_text(encoding=encoding)
        # Normalize CRLF to LF for consistent cross-platform handling
        if "\r\n" in text:
            text = text.replace("\r\n", "\n")
        lines = text.split("\n")
        total_lines = len(lines)
        start = max(0, offset - 1)
        end = len(lines) if limit is None else min(len(lines), start + limit)
        selected = lines[start:end]
        return {
            "content": "\n".join(selected),
            "encoding": encoding,
            "offset": offset,
            "limit": limit,
            "total_lines": total_lines,
        }
    else:
        with open(path, "rb") as f:
            return {"content": base64.b64encode(f.read()).decode(), "binary": True}


async def handle_file_write(params: dict[str, Any]) -> dict[str, Any]:
    """Write a file locally. Handles BOM+CRLF for Japanese source files."""
    path = Path(params["path"])
    content_b64 = params.get("content")
    encoding = params.get("encoding", "utf-8-sig")  # BOM-aware UTF-8
    newline = params.get("newline", "\r\n")  # Windows default

    if content_b64:
        # Fix base64 padding if necessary
        padding_needed = 4 - len(content_b64) % 4
        if padding_needed != 4:
            content_b64 += "=" * padding_needed
        raw = base64.b64decode(content_b64)
        with open(path, "wb") as f:
            f.write(raw)
    else:
        text = params.get("text", "")
        with open(path, "w", encoding=encoding, newline=newline) as f:
            f.write(text)

    return {"path": str(path), "bytesWritten": path.stat().st_size}


async def handle_file_delete(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(params["path"])
    path.unlink(missing_ok=True)
    return {"deleted": True, "path": str(path)}


async def handle_file_list(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(params["path"])
    entries = []
    for entry in path.iterdir():
        try:
            stat = entry.stat()
            entries.append({
                "name": entry.name,
                "isFile": entry.is_file(),
                "isDir": entry.is_dir(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
        except (PermissionError, OSError):
            entries.append({
                "name": entry.name,
                "isFile": False,
                "isDir": False,
                "size": 0,
                "mtime": 0,
                "error": "Permission denied",
            })
    return {"entries": entries, "path": str(path)}


def _sync_search_content(params: dict[str, Any]) -> dict[str, Any]:
    """Synchronous implementation of search.content (runs in thread pool)."""
    import re
    import fnmatch
    pattern_str = params["pattern"]
    search_path = Path(params.get("path", "."))
    file_glob = params.get("file_glob")
    limit = params.get("limit", 50)

    try:
        regex = re.compile(pattern_str)
    except re.error as e:
        return {"error": f"Invalid regex pattern: {e}"}

    matches = []
    count = 0
    files_scanned = 0
    MAX_FILES = 2000
    SKIP_DIRS = {'.git', 'node_modules', '.vs', 'bin', 'obj', 'packages', 'Debug', 'Release', 'x64', 'x86', 'arm64'}

    for root, dirs, files in os.walk(search_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        if count >= limit or files_scanned >= MAX_FILES:
            break
        for filename in files:
            if count >= limit or files_scanned >= MAX_FILES:
                break
            files_scanned += 1
            if file_glob and not fnmatch.fnmatch(filename, file_glob):
                continue
            file_path = Path(root) / filename
            try:
                stat = file_path.stat()
                if stat.st_size > 10 * 1024 * 1024:
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    try:
                        text = file_path.read_text(encoding="cp932")
                    except UnicodeDecodeError:
                        continue

                for line_num, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append({
                            "path": str(file_path),
                            "line": line_num,
                            "content": line.rstrip("\n\r").replace("\r\n", "\n"),
                        })
                        count += 1
                        if count >= limit:
                            break
            except (PermissionError, OSError):
                continue

    return {
        "matches": matches,
        "total": count,
        "pattern": pattern_str,
        "path": str(search_path),
        "files_scanned": files_scanned,
        "truncated": files_scanned >= MAX_FILES,
    }


async def handle_search_content(params: dict[str, Any]) -> dict[str, Any]:
    """Search file contents using Python standard library (no rg/grep dependency).
    Offloads to thread pool to avoid blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_search_content, params)


def _sync_search_files(params: dict[str, Any]) -> dict[str, Any]:
    """Synchronous implementation of search.files (runs in thread pool)."""
    import fnmatch
    search_path = Path(params.get("path", "."))
    pattern = params["pattern"]
    limit = params.get("limit", 50)

    results = []
    count = 0
    files_scanned = 0
    MAX_FILES = 5000
    SKIP_DIRS = {'.git', 'node_modules', '.vs', 'bin', 'obj', 'packages', 'Debug', 'Release', 'x64', 'x86', 'arm64'}

    for root, dirs, files in os.walk(search_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        if count >= limit or files_scanned >= MAX_FILES:
            break
        for filename in files:
            if count >= limit or files_scanned >= MAX_FILES:
                break
            files_scanned += 1
            if fnmatch.fnmatch(filename, pattern):
                file_path = Path(root) / filename
                try:
                    stat = file_path.stat()
                    results.append({
                        "path": str(file_path),
                        "isFile": file_path.is_file(),
                        "isDir": file_path.is_dir(),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
                    count += 1
                except (PermissionError, OSError):
                    results.append({
                        "path": str(file_path),
                        "isFile": False,
                        "isDir": False,
                        "size": 0,
                        "mtime": 0,
                        "error": "Permission denied",
                    })
                    count += 1

    return {
        "matches": results,
        "total": len(results),
        "pattern": pattern,
        "path": str(search_path),
        "files_scanned": files_scanned,
        "truncated": files_scanned >= MAX_FILES,
    }


async def handle_search_files(params: dict[str, Any]) -> dict[str, Any]:
    """Search files by name using Python standard library (no find/dir dependency).
    Offloads to thread pool to avoid blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_search_files, params)


async def handle_msbuild(params: dict[str, Any]) -> dict[str, Any]:
    """MSBuild wrapper with common defaults."""
    project = params["project"]
    configuration = params.get("configuration", "Release")
    platform = params.get("platform", "x64")
    targets = params.get("targets", "Build")
    cwd = params.get("cwd")

    msbuild_path = params.get("msbuildPath", "MSBuild.exe")
    cmd = (
        f'"{msbuild_path}" "{project}" '
        f"/p:Configuration={configuration} "
        f"/p:Platform={platform} "
        f"/t:{targets} "
        f"/m"
    )

    return await handle_terminal_exec({"cmd": cmd, "cwd": cwd, "timeoutMs": 300000})


async def handle_signtool(params: dict[str, Any]) -> dict[str, Any]:
    """SignTool wrapper. Uses Set-AuthenticodeSignature fallback if signtool path not given."""
    file_path = params["file"]
    thumbprint = params.get("thumbprint")
    hash_alg = params.get("hashAlgorithm", "SHA256")
    subject = params.get("subject")
    signtool_path = params.get("signtoolPath")

    if signtool_path and thumbprint:
        cmd = (
            f'"{signtool_path}" sign '
            f"/sha1 {thumbprint} "
            f"/fd {hash_alg} "
            f'"{file_path}"'
        )
    elif subject:
        cmd = (
            f'powershell.exe -Command "'
            f"$cert = Get-ChildItem Cert:\\LocalMachine\\My | "
            f"Where-Object {{ $_.Subject -eq 'CN={subject}' }} | Select-Object -First 1; "
            f"Set-AuthenticodeSignature -FilePath '{file_path}' -Certificate $cert -HashAlgorithm {hash_alg}"
            f'"'
        )
    else:
        raise ValueError("thumbprint+signtoolPath or subject required")

    return await handle_terminal_exec({"cmd": cmd, "timeoutMs": 60000})


async def handle_lsp(params: dict[str, Any]) -> dict[str, Any]:
    """LSP diagnostics via local language server.

    Expects params:
        action: "lint_after_write" | "get_diagnostics" | "shutdown"
        language: "csharp" | "python" | "typescript" | ...
        workspace_root: str (optional, defaults to cwd)
        file_path: str
        content: str (optional, for lint_after_write)
    """
    try:
        from node_client.lsp_server import get_lsp_manager
    except ImportError:
        # Fallback: try relative import if lsp_server.py is in same dir
        import importlib.util
        script_dir = Path(__file__).parent
        spec = importlib.util.spec_from_file_location("lsp_server", script_dir / "lsp_server.py")
        if spec and spec.loader:
            lsp_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(lsp_mod)
            get_lsp_manager = lsp_mod.get_lsp_manager
        else:
            # Second fallback: node_client/ sibling directory
            spec2 = importlib.util.spec_from_file_location(
                "lsp_server", script_dir.parent / "node_client" / "lsp_server.py"
            )
            if spec2 and spec2.loader:
                lsp_mod = importlib.util.module_from_spec(spec2)
                spec2.loader.exec_module(lsp_mod)
                get_lsp_manager = lsp_mod.get_lsp_manager
            else:
                return {"error": "lsp_server.py not found"}

    mgr = get_lsp_manager()
    return await mgr.handle_request(params)


async def handle_node_restart(params: dict[str, Any]) -> dict[str, Any]:
    """Restart the node client process. Spawns a new process then exits self."""
    import subprocess
    import sys
    import os

    # Get the path to this script
    script_path = os.path.abspath(__file__)

    # Build the same command line that was used to start this process
    # Use pythonw on Windows to avoid console window
    python_exe = sys.executable
    if sys.platform == "win32" and not python_exe.lower().endswith("pythonw.exe"):
        python_exe = python_exe.replace("python.exe", "pythonw.exe")

    args = [python_exe, script_path]

    # Pass through original CLI args if available, otherwise use env defaults
    # We can't easily access original CLI args here, so use the connect params
    # The new process will reconnect with the same config

    logger.info("[%s] Spawning restart process: %s", NODE_ID, args)

    # Start new process detached (no console window on Windows)
    if sys.platform == "win32":
        subprocess.Popen(
            args,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            ),
            close_fds=True,
        )
    else:
        subprocess.Popen(
            args,
            start_new_session=True,
            close_fds=True,
        )

    # Schedule self-termination after sending response
    async def _delayed_exit():
        await asyncio.sleep(1)
        logger.info("[%s] Exiting for restart.", NODE_ID)
        os._exit(0)

    asyncio.create_task(_delayed_exit())

    return {"restarting": True, "nodeId": NODE_ID}


# ---------------------------------------------------------------------------
# Terminal stream (bidirectional proxy)
# ---------------------------------------------------------------------------

TERMINAL_SESSIONS: dict[str, dict[str, Any]] = {}

# Inactivity timeout for terminal sessions (5 minutes)
TERMINAL_INACTIVITY_TIMEOUT_SEC = 300

async def _terminal_cleanup_inactive() -> None:
    """Background task: close terminal sessions idle for too long."""
    while True:
        await asyncio.sleep(60)  # Check every minute
        now = time.time()
        to_close = []
        for proxy_id, session in list(TERMINAL_SESSIONS.items()):
            last = session.get("last_activity", 0)
            if now - last > TERMINAL_INACTIVITY_TIMEOUT_SEC:
                to_close.append(proxy_id)
        for proxy_id in to_close:
            logger.info("[%s] Terminal session %s inactive for %ds, closing",
                        NODE_ID, proxy_id, TERMINAL_INACTIVITY_TIMEOUT_SEC)
            await _terminal_close(proxy_id)

# Start the cleanup task when module loads
_terminal_cleanup_task = None

def _ensure_cleanup_task() -> None:
    global _terminal_cleanup_task
    if _terminal_cleanup_task is None or _terminal_cleanup_task.done():
        _terminal_cleanup_task = asyncio.create_task(_terminal_cleanup_inactive())


async def handle_terminal_stream(params: dict[str, Any]) -> dict[str, Any]:
    """Handle terminal stream actions: open, write, resize, close.

    This is a command handler for the request/response pattern.
    The actual streaming happens via WebSocket events after the
    terminal session is opened.
    """
    proxy_id = params["proxyId"]
    action = params["action"]

    logger.info("[%s] handle_terminal_stream: proxy_id=%s action=%s", NODE_ID, proxy_id, action)

    if action == "open":
        return await _terminal_open(proxy_id, params)
    elif action == "write":
        return await _terminal_write(proxy_id, params)
    elif action == "resize":
        return await _terminal_resize(proxy_id, params)
    elif action == "close":
        return await _terminal_close(proxy_id)
    else:
        return {"ok": False, "error": f"Unknown action: {action}"}


async def _terminal_open(proxy_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Open a new terminal session. Enforce only 1 PTY per node."""
    # Close ALL existing sessions on this node (enforce 1 PTY per node)
    for existing_id in list(TERMINAL_SESSIONS.keys()):
        await _terminal_close(existing_id)

    shell = params.get("shell")
    if not shell:
        shell = "powershell.exe" if sys.platform == "win32" else "bash"

    cols = params.get("cols", 80)
    rows = params.get("rows", 24)
    initial_cwd = params.get("cwd")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if sys.platform == "win32":
        env["TERM"] = "xterm-256color"

    try:
        # Use PTY on Unix for interactive shell (prompt, colors, etc.)
        if sys.platform != "win32" and shell in ("bash", "sh", "zsh"):
            import pty
            import termios
            import struct
            import fcntl

            master_fd, slave_fd = pty.openpty()

            # Configure terminal attributes BEFORE starting child process
            # (settings are inherited by child via passed fd)
            try:
                attrs = termios.tcgetattr(slave_fd)
                # Enable echo and canonical mode
                attrs[3] |= termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
                # Enable input processing
                attrs[0] |= termios.ICRNL
                # Enable output processing
                attrs[1] |= termios.OPOST | termios.ONLCR
                termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
            except Exception:
                pass

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["TERM"] = "xterm-256color"
            env["STTY"] = "echo icanon isig icrnl onlcr opost"
            # Create empty inputrc to disable readline custom bindings that may reset termios
            env["INPUTRC"] = "/dev/null"

            # Use asyncio subprocess with PTY (avoids fork issues in event loop)
            # --norc = don't read .bashrc, -i = interactive; INPUTRC=/dev/null prevents readline from resetting termios
            proc = await asyncio.create_subprocess_exec(
                shell, "--norc", "-i",
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                pass_fds=[slave_fd],
                env=env,
                start_new_session=True,
            )

            os.close(slave_fd)

            # Re-apply termios settings to the child's controlling terminal
            # (bash resets them on startup)
            try:
                time.sleep(0.1)  # Wait for child to start
                # Get slave PTY name from master_fd
                try:
                    pts_name = os.ptsname(master_fd)
                except AttributeError:
                    # Fallback for Python < 3.9
                    import ctypes
                    libc = ctypes.CDLL('libc.so.6')
                    libc.ptsname.restype = ctypes.c_char_p
                    pts_name = libc.ptsname(master_fd).decode('utf-8')
                logger.info("[%s] PTY post-start: pts_name=%s", NODE_ID, pts_name)
                pts_fd = os.open(pts_name, os.O_RDWR | os.O_NOCTTY)
                try:
                    attrs = termios.tcgetattr(pts_fd)
                    attrs[3] |= termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
                    attrs[0] |= termios.ICRNL
                    attrs[1] |= termios.OPOST | termios.ONLCR
                    termios.tcsetattr(pts_fd, termios.TCSANOW, attrs)
                    logger.info("[%s] PTY post-start: termios applied to %s successfully", NODE_ID, pts_name)
                finally:
                    os.close(pts_fd)
            except Exception as e:
                logger.warning("[%s] PTY post-start: failed to apply termios: %s", NODE_ID, e)

            # Set terminal size
            try:
                size = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
            except Exception:
                pass

            logger.info("[%s] PTY session %s opened (master_fd=%d, shell=%s, pid=%d)", NODE_ID, proxy_id, master_fd, shell, proc.pid)
            TERMINAL_SESSIONS[proxy_id] = {
                "proc": proc,
                "master_fd": master_fd,
                "cols": cols,
                "rows": rows,
                "shell": shell,
                "pty": True,
                "last_activity": time.time(),
                "reader_task": None,
            }
            _ensure_cleanup_task()

            logger.info("[%s] Terminal session %s opened (PTY, shell=%s)", NODE_ID, proxy_id, shell)
            return {"ok": True, "proxyId": proxy_id}

        # Windows PTY mode using pywinpty (ConPTY)
        if sys.platform == "win32":
            try:
                from winpty import PTY

                # PowerShell works best with -NoExit to keep the session alive
                # Use -WorkingDirectory to set the initial cwd (more reliable than pty.spawn cwd)
                appname = shell
                cmdline_args = "-NoExit"
                if initial_cwd:
                    cmdline_args += f' -WorkingDirectory "{initial_cwd}"'
                cmdline_args += ' -Command "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8"'

                pty = PTY(cols, rows)
                pty.spawn(appname, cmdline=cmdline_args)

                logger.info("[%s] WinPTY session %s opened (shell=%s, cwd=%s)", NODE_ID, proxy_id, shell, initial_cwd)
                TERMINAL_SESSIONS[proxy_id] = {
                    "pty": True,
                    "winpty": pty,
                    "cols": cols,
                    "rows": rows,
                    "shell": shell,
                    "last_activity": time.time(),
                    "reader_task": None,
                }
                _ensure_cleanup_task()
                return {"ok": True, "proxyId": proxy_id}
            except ImportError:
                logger.warning("[%s] pywinpty not installed, falling back to pipe mode", NODE_ID)
            except Exception as e:
                logger.warning("[%s] WinPTY init failed: %s, falling back to pipe mode", NODE_ID, e)

        # Fallback: regular subprocess (Windows without pywinpty, or non-shell commands)
        proc = await asyncio.create_subprocess_exec(
            shell,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except Exception as e:
        logger.exception("[%s] Failed to start terminal", NODE_ID)
        return {"ok": False, "error": str(e)}

    TERMINAL_SESSIONS[proxy_id] = {
        "proc": proc,
        "cols": cols,
        "rows": rows,
        "shell": shell,
        "pty": False,
        "last_activity": time.time(),
    }
    _ensure_cleanup_task()

    logger.info("[%s] Terminal session %s opened (shell=%s)", NODE_ID, proxy_id, shell)
    return {"ok": True, "proxyId": proxy_id}


async def _terminal_write(proxy_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Write data to terminal stdin."""
    session = TERMINAL_SESSIONS.get(proxy_id)
    if not session:
        return {"ok": False, "error": "Terminal session not found"}

    data_b64 = params.get("data", "")
    try:
        data = base64.b64decode(data_b64).decode("utf-8", errors="replace")
    except Exception:
        data = data_b64  # fallback: treat as plain text

    session["last_activity"] = time.time()

    # Windows PTY mode (pywinpty)
    winpty = session.get("winpty")
    if winpty is not None:
        try:
            winpty.write(data)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"Write failed: {e}"}

    proc = session["proc"]
    master_fd = session.get("master_fd")
    logger.info("[%s] _terminal_write: proxy_id=%s master_fd=%s data_len=%d", NODE_ID, proxy_id, master_fd, len(data))
    if master_fd is not None:
        # PTY mode: write directly to master_fd
        try:
            os.write(master_fd, data.encode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"Write failed: {e}"}
    elif proc.stdin and not proc.stdin.is_closing():
        try:
            proc.stdin.write(data.encode("utf-8"))
            await proc.stdin.drain()
        except Exception as e:
            return {"ok": False, "error": f"Write failed: {e}"}

    return {"ok": True}


async def _terminal_resize(proxy_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Resize terminal (best effort)."""
    session = TERMINAL_SESSIONS.get(proxy_id)
    if not session:
        return {"ok": False, "error": "Terminal session not found"}

    cols = params.get("cols", 80)
    rows = params.get("rows", 24)
    session["cols"] = cols
    session["rows"] = rows

    # Windows PTY mode (pywinpty)
    winpty = session.get("winpty")
    if winpty is not None:
        try:
            winpty.set_size(cols, rows)
            return {"ok": True}
        except Exception as e:
            logger.debug("[%s] WinPTY resize failed: %s", NODE_ID, e)
            return {"ok": True}  # best effort

    # On Unix PTY, resizing requires termios
    master_fd = session.get("master_fd")
    if master_fd is not None and sys.platform != "win32":
        try:
            import struct
            import termios
            import fcntl
            size = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
        except Exception:
            pass

    return {"ok": True}


async def _terminal_close(proxy_id: str) -> dict[str, Any]:
    """Close a terminal session. Supports __all__ to close all sessions."""
    if proxy_id == "__all__":
        for existing_id in list(TERMINAL_SESSIONS.keys()):
            await _terminal_close(existing_id)
        return {"ok": True}

    session = TERMINAL_SESSIONS.pop(proxy_id, None)
    if not session:
        return {"ok": True}  # Already closed

    proc = session.get("proc")
    master_fd = session.get("master_fd")
    reader_task = session.get("reader_task")
    winpty = session.get("winpty")

    # Cancel reader task first and wait for it to finish
    if reader_task is not None:
        try:
            reader_task.cancel()
            await reader_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[%s] Error cancelling reader task for %s: %s", NODE_ID, proxy_id, e)

    # Close Windows PTY
    if winpty is not None:
        try:
            winpty.close()
            logger.info("[%s] Terminal session %s WinPTY closed", NODE_ID, proxy_id)
        except Exception as e:
            logger.debug("[%s] Error closing WinPTY for %s: %s", NODE_ID, proxy_id, e)

    # Close master_fd after reader task has exited
    if master_fd is not None:
        try:
            os.close(master_fd)
            logger.info("[%s] Terminal session %s master_fd=%d closed", NODE_ID, proxy_id, master_fd)
        except Exception as e:
            logger.debug("[%s] Error closing master_fd for %s: %s", NODE_ID, proxy_id, e)

    try:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    except Exception as e:
        logger.debug("[%s] Error terminating process for %s: %s", NODE_ID, proxy_id, e)

    return {"ok": True}


async def _read_terminal_output(
    ws: websockets.WebSocketClientProtocol,
    proxy_id: str,
    proc: asyncio.subprocess.Process,
) -> None:
    """Background task: read terminal output and send to gateway."""
    session = TERMINAL_SESSIONS.get(proxy_id)
    if session and session.get("pty") and session.get("master_fd") is not None:
        # PTY mode: read from master_fd using os.read in executor
        await _read_terminal_output_pty(ws, proxy_id, session["master_fd"], proc)
        return

    async def read_stream(stream, name: str) -> None:
        if stream is None:
            return
        while True:
            try:
                data = await stream.read(4096)
            except Exception:
                break
            if not data:
                break
            try:
                await ws.send(json.dumps({
                    "type": "event",
                    "event": "node.terminal.output",
                    "payload": {
                        "proxyId": proxy_id,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }))
            except Exception as exc:
                logger.debug("[%s] Terminal output send failed: %s", NODE_ID, exc)
                break

    # Run both stdout and stderr readers concurrently
    await asyncio.gather(
        read_stream(proc.stdout, "stdout"),
        read_stream(proc.stderr, "stderr"),
        return_exceptions=True,
    )

    # Process exited — notify gateway
    try:
        await ws.send(json.dumps({
            "type": "event",
            "event": "node.terminal.close",
            "payload": {"proxyId": proxy_id},
        }))
    except Exception:
        pass

    # Clean up session
    TERMINAL_SESSIONS.pop(proxy_id, None)


async def _read_terminal_output_pty(
    ws: websockets.WebSocketClientProtocol,
    proxy_id: str,
    master_fd: int,
    proc: asyncio.subprocess.Process,
) -> None:
    """Background task: read PTY master_fd output and send to gateway."""
    loop = asyncio.get_event_loop()
    logger.info("[%s] PTY reader started for proxy_id=%s master_fd=%d", NODE_ID, proxy_id, master_fd)
    check_count = 0
    try:
        while True:
            try:
                # Check if process has exited
                if proc.returncode is not None:
                    logger.info("[%s] PTY reader: process exited, breaking", NODE_ID)
                    break
                # Read from master_fd with timeout to avoid blocking forever
                data = await asyncio.wait_for(
                    loop.run_in_executor(None, os.read, master_fd, 4096),
                    timeout=0.5,
                )
                if not data:
                    logger.info("[%s] PTY reader: EOF", NODE_ID)
                    break
                logger.info("[%s] PTY reader: read %d bytes", NODE_ID, len(data))
                # Periodically re-apply termios settings (bash/readline may reset them)
                check_count += 1
                if check_count % 2 == 0:  # Check every 2 reads (was 10)
                    try:
                        pts_name = os.ptsname(master_fd)
                        pts_fd = os.open(pts_name, os.O_RDWR | os.O_NOCTTY)
                        try:
                            attrs = termios.tcgetattr(pts_fd)
                            if not (attrs[3] & termios.ECHO):
                                attrs[3] |= termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
                                attrs[0] |= termios.ICRNL
                                attrs[1] |= termios.OPOST | termios.ONLCR
                                termios.tcsetattr(pts_fd, termios.TCSANOW, attrs)
                                logger.info("[%s] PTY watchdog: re-enabled echo on %s", NODE_ID, pts_name)
                        finally:
                            os.close(pts_fd)
                    except Exception:
                        pass
                try:
                    session = TERMINAL_SESSIONS.get(proxy_id)
                    if session:
                        session["last_activity"] = time.time()
                    await ws.send(json.dumps({
                        "type": "event",
                        "event": "node.terminal.output",
                        "payload": {
                            "proxyId": proxy_id,
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }))
                    logger.info("[%s] PTY reader: sent %d bytes to gateway", NODE_ID, len(data))
                except Exception as exc:
                    logger.warning("[%s] Terminal output send failed: %s", NODE_ID, exc)
                    break
            except asyncio.TimeoutError:
                # No output available, loop back and check process status
                continue
            except OSError as exc:
                logger.warning("[%s] PTY reader OSError: %s", NODE_ID, exc)
                break
    except Exception as exc:
        logger.warning("[%s] PTY reader error: %s", NODE_ID, exc)

    # Process exited — notify gateway
    try:
        await ws.send(json.dumps({
            "type": "event",
            "event": "node.terminal.close",
            "payload": {"proxyId": proxy_id},
        }))
    except Exception:
        pass

    # Clean up session (do NOT close master_fd here — _terminal_close handles it)
    TERMINAL_SESSIONS.pop(proxy_id, None)


async def _read_terminal_output_winpty(
    ws: websockets.WebSocketClientProtocol,
    proxy_id: str,
    winpty: Any,
) -> None:
    """Background task: read Windows PTY output and send to gateway.

    pywinpty PTY.read() is synchronous, so we run it in an executor.
    """
    logger.info("[%s] WinPTY reader started for proxy_id=%s", NODE_ID, proxy_id)
    loop = asyncio.get_running_loop()

    try:
        while True:
            session = TERMINAL_SESSIONS.get(proxy_id)
            if not session or session.get("winpty") is None:
                logger.info("[%s] WinPTY reader: session closed, breaking", NODE_ID)
                break

            try:
                # PTY.read() blocks until data is available; use executor
                # pywinpty 3.x: read(blocking=False) returns str, no length param
                data = await loop.run_in_executor(None, lambda: winpty.read(blocking=True))
            except Exception as exc:
                logger.info("[%s] WinPTY reader: read error %s", NODE_ID, exc)
                break

            if not data:
                await asyncio.sleep(0.05)
                continue

            try:
                if session:
                    session["last_activity"] = time.time()
                await ws.send(json.dumps({
                    "type": "event",
                    "event": "node.terminal.output",
                    "payload": {
                        "proxyId": proxy_id,
                        "data": base64.b64encode(data.encode("utf-8", errors="replace")).decode("ascii"),
                    },
                }))
                logger.info("[%s] WinPTY reader: sent %d bytes to gateway", NODE_ID, len(data))
            except Exception as exc:
                logger.warning("[%s] WinPTY reader: send failed: %s", NODE_ID, exc)
                break
    except Exception as exc:
        logger.warning("[%s] WinPTY reader error: %s", NODE_ID, exc)

    # Notify gateway
    try:
        await ws.send(json.dumps({
            "type": "event",
            "event": "node.terminal.close",
            "payload": {"proxyId": proxy_id},
        }))
    except Exception:
        pass

    # Clean up session
    TERMINAL_SESSIONS.pop(proxy_id, None)


async def handle_terminal_data(ws: websockets.WebSocketClientProtocol, payload: dict[str, Any]) -> None:
    """Handle terminal data events from gateway (open/write/resize/close)."""
    proxy_id = payload.get("proxyId")
    action = payload.get("action")

    if not proxy_id:
        return

    logger.info("[%s] handle_terminal_data: proxy_id=%s action=%s", NODE_ID, proxy_id, action)

    if action == "open":
        result = await _terminal_open(proxy_id, payload)
        if result["ok"]:
            # Start background output reader
            session = TERMINAL_SESSIONS.get(proxy_id)
            if session:
                if session.get("winpty") is not None:
                    task = asyncio.create_task(_read_terminal_output_winpty(ws, proxy_id, session["winpty"]))
                    session["reader_task"] = task
                elif session.get("pty") and session.get("master_fd") is not None:
                    task = asyncio.create_task(_read_terminal_output_pty(ws, proxy_id, session["master_fd"], session["proc"]))
                    session["reader_task"] = task
                else:
                    asyncio.create_task(_read_terminal_output(ws, proxy_id, session["proc"]))
        else:
            await _send_terminal_error(ws, proxy_id, result.get("error", "Open failed"))
    elif action == "write":
        result = await _terminal_write(proxy_id, payload)
        if not result["ok"]:
            await _send_terminal_error(ws, proxy_id, result.get("error", "Write failed"))
    elif action == "resize":
        await _terminal_resize(proxy_id, payload)
    elif action == "close":
        await _terminal_close(proxy_id)


async def _send_terminal_error(ws: websockets.WebSocketClientProtocol, proxy_id: str, error: str) -> None:
    """Send terminal error to gateway."""
    try:
        await ws.send(json.dumps({
            "type": "event",
            "event": "node.terminal.error",
            "payload": {"proxyId": proxy_id, "error": error},
        }))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

HANDLERS = {
    "terminal.exec": handle_terminal_exec,
    "terminal.stream": handle_terminal_stream,
    "file.read": handle_file_read,
    "file.write": handle_file_write,
    "file.delete": handle_file_delete,
    "file.list": handle_file_list,
    "search.content": handle_search_content,
    "search.files": handle_search_files,
    "msbuild": handle_msbuild,
    "signtool": handle_signtool,
    "node.restart": handle_node_restart,
    "lsp": handle_lsp,
}


async def handle_invoke(ws: websockets.WebSocketClientProtocol, payload: dict[str, Any]) -> None:
    request_id = payload["id"]
    command = payload["command"]
    params = json.loads(payload.get("paramsJSON") or "{}")

    logger.info("[%s] Invoking %s", NODE_ID, command)
    try:
        handler = HANDLERS.get(command)
        if not handler:
            raise ValueError(f"Unknown command: {command}")

        result = await handler(params)

        # For terminal.stream open, start background output reader
        if command == "terminal.stream" and result.get("ok") and params.get("action") == "open":
            proxy_id = params.get("proxyId")
            session = TERMINAL_SESSIONS.get(proxy_id)
            if session and proxy_id:
                if session.get("winpty") is not None:
                    task = asyncio.create_task(_read_terminal_output_winpty(ws, proxy_id, session["winpty"]))
                    session["reader_task"] = task
                elif session.get("pty") and session.get("master_fd") is not None:
                    task = asyncio.create_task(_read_terminal_output_pty(ws, proxy_id, session["master_fd"], session["proc"]))
                    session["reader_task"] = task
                else:
                    asyncio.create_task(_read_terminal_output(ws, proxy_id, session["proc"]))

        await send_result(ws, request_id, True, result)
    except Exception as e:
        logger.exception("[%s] Command %s failed", NODE_ID, command)
        await send_result(ws, request_id, False, None, str(e))


async def send_result(
    ws: websockets.WebSocketClientProtocol,
    request_id: str,
    ok: bool,
    payload: Any = None,
    error: str | None = None,
) -> None:
    await ws.send(json.dumps({
        "type": "event",
        "event": "node.invoke.result",
        "payload": {
            "id": request_id,
            "nodeId": NODE_ID,
            "ok": ok,
            "payload": payload,
            "error": {"message": error} if error else None,
        }
    }))


# ---------------------------------------------------------------------------
# Connection logic with auto-reconnect
# ---------------------------------------------------------------------------

async def _send_connect_message(ws, node_id: str, token: str) -> bool:
    """Send OpenClaw-style connect handshake and wait for response."""
    connect_req = {
        "type": "req",
        "id": str(uuid.uuid4()),
        "method": "connect",
        "params": {
            "minProtocol": 1,
            "maxProtocol": 1,
            "client": {
                "id": node_id,
                "version": "1.0.0",
                "platform": "windows" if sys.platform == "win32" else "linux",
                "mode": "node",
            },
            "role": "node",
            "scopes": [],
            "caps": ["terminal", "file", "build"],
            "commands": sorted(COMMANDS),
            "auth": {"token": token},
        }
    }
    await ws.send(json.dumps(connect_req))

    response = await ws.recv()
    data = json.loads(response)
    if data.get("type") == "res" and data.get("ok"):
        logger.info("[%s] Connected. Waiting for commands...", node_id)
        return True
    else:
        logger.error("[%s] Handshake failed: %s", node_id, data)
        return False


async def _connection_loop(ws, node_id: str) -> None:
    """Main message loop after successful handshake."""
    while True:
        try:
            msg = await ws.recv()
            data = json.loads(msg)
        except websockets.ConnectionClosed:
            logger.warning("[%s] Connection closed by server.", node_id)
            break

        if data.get("type") == "event" and data.get("event") == "node.invoke.request":
            asyncio.create_task(handle_invoke(ws, data["payload"]))
        elif data.get("type") == "event" and data.get("event") == "node.terminal.data":
            asyncio.create_task(handle_terminal_data(ws, data["payload"]))
        elif data.get("type") == "event" and data.get("event") == "ping":
            # Keepalive ping
            pass


async def connect_and_serve(gateway_url: str, token: str, node_id: str, api_server_url: str = "") -> None:
    """Connect to API Server with exponential backoff reconnect.

    Always uses API Server mode. The gateway_url parameter is kept for
    backward compatibility but is ignored.
    """
    delay = RECONNECT_MIN_DELAY

    # Always use API Server mode
    target_url = api_server_url if api_server_url else gateway_url
    mode = "api_server"

    # Build authenticated WebSocket URL with ?token=xxx query param
    separator = "&" if "?" in target_url else "?"
    auth_url = f"{target_url}{separator}token={token}"

    while True:
        try:
            logger.info("[%s] Connecting to %s (%s mode)...", node_id, target_url, mode)
            async with websockets.connect(auth_url) as ws:
                delay = RECONNECT_MIN_DELAY  # Reset on successful connect

                # --- Handshake ---
                if not await _send_connect_message(ws, node_id, token):
                    await asyncio.sleep(delay)
                    delay = min(delay * RECONNECT_BACKOFF_MULTIPLIER, RECONNECT_MAX_DELAY)
                    continue

                # --- Main loop ---
                await _connection_loop(ws, node_id)

        except (websockets.ConnectionClosed, websockets.ConnectionClosedOK, websockets.ConnectionClosedError) as exc:
            logger.warning("[%s] Connection closed: %s", node_id, exc)
        except OSError as exc:
            logger.error("[%s] Connection error: %s", node_id, exc)
        except Exception as exc:
            logger.exception("[%s] Unexpected error in connection loop", node_id)

        logger.info("[%s] Reconnecting in %.1f seconds...", node_id, delay)
        await asyncio.sleep(delay)
        delay = min(delay * RECONNECT_BACKOFF_MULTIPLIER, RECONNECT_MAX_DELAY)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Remote Node Client")
    parser.add_argument("--gateway", default=GATEWAY_URL, help="Deprecated: kept for compatibility, ignored")
    parser.add_argument("--api-server", default=API_SERVER_URL, help="API Server WebSocket URL (default: ws://localhost:8642/ws)")
    parser.add_argument("--token", default=NODE_TOKEN, help="Node authentication token")
    parser.add_argument("--node-id", default=NODE_ID, help="Unique node identifier")
    args = parser.parse_args()

    # Always use API Server URL
    api_server_url = args.api_server if args.api_server else args.gateway

    try:
        asyncio.run(connect_and_serve(args.gateway, args.token, args.node_id, api_server_url))
    except KeyboardInterrupt:
        logger.info("[%s] Shutting down.", args.node_id)


if __name__ == "__main__":
    main()
