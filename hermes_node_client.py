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
import importlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

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
        with open(env_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ[key] = value
    except Exception:
        pass

_load_dotenv()


def _default_node_id() -> str:
    host = (socket.gethostname() or "").strip()
    if not host:
        return "node-local"
    return host.split(".", 1)[0]

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
NODE_ID = os.environ.get("HERMES_NODE_ID", _default_node_id())
GATEWAY_URL = os.environ.get("HERMES_GATEWAY_URL", "ws://localhost:8642/ws")
API_SERVER_URL = os.environ.get("HERMES_API_SERVER_URL", "")
NODE_TOKEN = os.environ.get("HERMES_NODE_TOKEN", "dev-token-change-me")

RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_BROWSER_DEBUG_PORT = 9222

BASE_COMMANDS = {
    "terminal.exec",
    "terminal.stream",
    "file.read",
    "file.write",
    "file.patch",
    "file.delete",
    "file.list",
    "search.content",
    "search.files",
    "msbuild",
    "signtool",
    "node.restart",
    "lsp",
}
BROWSER_COMMANDS = {
    "browser.debug_status",
    "browser.debug_launch",
}
COMMANDS = set(BASE_COMMANDS) | set(BROWSER_COMMANDS)
NODE_METADATA = {
    "browser_scope": "full_browser",
    "computer_use_enabled": False,
}

DEFAULT_TIMEOUT_SEC = 300
MAX_TIMEOUT_SEC = 600


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_terminal_exec(params: dict[str, Any]) -> dict[str, Any]:
    """Execute PowerShell locally. No SSH escaping hell."""
    cmd = params["cmd"]
    cwd = params.get("cwd")
    raw_timeout_ms = params.get("timeoutMs")
    if raw_timeout_ms is None and "timeout" in params:
        try:
            raw_timeout_ms = float(params.get("timeout")) * 1000
        except (TypeError, ValueError):
            raw_timeout_ms = None
    if raw_timeout_ms is None:
        raw_timeout_ms = DEFAULT_TIMEOUT_SEC * 1000
    try:
        timeout_ms = float(raw_timeout_ms)
    except (TypeError, ValueError):
        timeout_ms = DEFAULT_TIMEOUT_SEC * 1000
    timeout_sec = min(max(timeout_ms / 1000, 1), MAX_TIMEOUT_SEC)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # On Windows: use PowerShell with UTF-8 encoding, bypassing cmd.exe AutoRun
    # Skip wrapping if the command already invokes PowerShell/pwsh directly
    if sys.platform == "win32" and not cmd.strip().lower().startswith(("powershell", "pwsh")):
        # Use -EncodedCommand to avoid ALL escaping issues (\t, $, `, ", etc.).
        # The script is Base64-encoded UTF-16 LE, so no shell metacharacter
        # interpretation happens at the OS command-line level.
        ps_script = (
            '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
            '$OutputEncoding = [System.Text.Encoding]::UTF8; '
            '$ErrorView = "NormalView"; '
            f'{cmd}'
        )
        encoded = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')
        args = [
            _resolve_command("powershell"), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", encoded,
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
    This strips the '#< CLIXML' header and the entire XML block that follows,
    but extracts human-readable error messages from <S S="Error"> elements.
    """
    import re

    # Fast path: no CLIXML at all
    if '#<' not in text and '#<CLIXML' not in text:
        return text

    # Extract error strings from <S S="Error">...elements
    error_strings = re.findall(
        r'<S\s+S=["\']Error["\']\s*>(.*?)</S>',
        text,
        re.DOTALL | re.IGNORECASE
    )

    if error_strings:
        # Decode _x000D_ / _x000A_ entities used by PowerShell CLIXML
        decoded = []
        for s in error_strings:
            s = s.replace('_x000D_', '\r').replace('_x000A_', '\n')
            # Skip lines that are just the script preamble (not actual errors)
            skip_prefixes = (
                '[Console]::OutputEncoding =',
                '$OutputEncoding =',
                '$ErrorView =',
                '"NormalView"',
            )
            if s.strip() and not any(s.strip().startswith(p) for p in skip_prefixes):
                decoded.append(s)
        return '\n'.join(decoded).strip()

    # Fallback: strip the entire XML block
    pattern = re.compile(r'#<\s*CLIXML\s*<Objs[^>]*>.*?</Objs>', re.DOTALL | re.IGNORECASE)
    cleaned = pattern.sub('', text)
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


def _guess_public_host() -> str:
    """Best-effort LAN/Tailscale-reachable host for browser CDP handoff."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
            if host:
                return host
    except OSError:
        pass
    try:
        host = socket.gethostbyname(socket.gethostname())
        if host:
            return host
    except OSError:
        pass
    return "127.0.0.1"


def _browser_command_groups() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    if sys.platform == "win32":
        return {
            "chrome": (("chrome.exe",), (
                os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            )),
            "edge": (("msedge.exe",), (
                os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            )),
            "brave": (("brave.exe", "brave-browser.exe"), (
                os.path.join(os.environ.get("ProgramFiles", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            )),
            "chromium": (("chromium.exe", "chromium-browser.exe"), (
                os.path.join(os.environ.get("ProgramFiles", ""), "Chromium", "Application", "chrome.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Chromium", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Chromium", "Application", "chrome.exe"),
            )),
        }
    if sys.platform == "darwin":
        return {
            "chrome": (("Google Chrome",), ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",)),
            "edge": (("Microsoft Edge",), ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",)),
            "brave": (("Brave Browser",), ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",)),
            "chromium": (("Chromium",), ("/Applications/Chromium.app/Contents/MacOS/Chromium",)),
        }
    return {
        "chrome": (("google-chrome", "google-chrome-stable"), ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/opt/google/chrome/chrome")),
        "edge": (("microsoft-edge", "microsoft-edge-stable", "msedge"), ("/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable", "/opt/microsoft/msedge/msedge", "/opt/microsoft/msedge/microsoft-edge")),
        "brave": (("brave-browser", "brave-browser-stable", "brave"), ("/usr/bin/brave-browser", "/usr/bin/brave-browser-stable", "/usr/bin/brave", "/snap/bin/brave")),
        "chromium": (("chromium", "chromium-browser"), ("/usr/bin/chromium", "/usr/bin/chromium-browser")),
    }


def _find_browser_executable(browser: str) -> Optional[str]:
    groups = _browser_command_groups()
    ordered = [browser] if browser in groups else ["chrome", "edge", "brave", "chromium"]
    seen: set[str] = set()
    for key in ordered:
        names, paths = groups.get(key, ((), ()))
        for name in names:
            candidate = shutil.which(name)
            if candidate and candidate not in seen:
                return candidate
            seen.add(candidate or name)
        for path in paths:
            if path and os.path.isfile(path) and path not in seen:
                return path
            seen.add(path)
    return None


def _browser_detach_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}


def _rewrite_ws_host(ws_url: str, host: str) -> str:
    parsed = urllib.parse.urlparse(ws_url)
    if not parsed.scheme or not parsed.netloc or not host:
        return ws_url
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    return parsed._replace(netloc=f"{host}:{port}").geturl()


def _read_browser_debug_payload(host: str, port: int) -> dict[str, Any]:
    version_url = f"http://{host}:{port}/json/version"
    json_url = f"http://{host}:{port}/json"
    with urllib.request.urlopen(version_url, timeout=2.0) as response:
        payload = json.load(response)
    try:
        with urllib.request.urlopen(json_url, timeout=2.0) as response:
            targets = json.load(response)
    except Exception:
        targets = []
    if not isinstance(targets, list):
        targets = []
    payload["_target_count"] = len(targets)
    return payload


def _browser_debug_status_payload(port: int, discovery_host: str = "127.0.0.1", public_host: Optional[str] = None) -> dict[str, Any]:
    try:
        payload = _read_browser_debug_payload(discovery_host, port)
    except Exception as exc:
        return {
            "listening": False,
            "host": discovery_host,
            "port": port,
            "discovery_url": f"http://{discovery_host}:{port}",
            "suggested_connect_url": "",
            "error": str(exc),
        }

    ws_url = str(payload.get("webSocketDebuggerUrl") or "")
    remote_host = public_host or _guess_public_host()
    suggested = _rewrite_ws_host(ws_url, remote_host) if ws_url and remote_host else ws_url
    return {
        "listening": True,
        "host": discovery_host,
        "port": port,
        "browser": payload.get("Browser", ""),
        "protocol_version": payload.get("Protocol-Version", ""),
        "user_agent": payload.get("User-Agent", ""),
        "target_count": payload.get("_target_count", 0),
        "discovery_url": f"http://{discovery_host}:{port}",
        "websocket_debugger_url": ws_url,
        "suggested_connect_url": suggested,
        "public_host": remote_host,
    }


async def handle_browser_debug_status(params: dict[str, Any]) -> dict[str, Any]:
    """Return the current browser CDP endpoint information on this node."""
    port = int(params.get("port") or DEFAULT_BROWSER_DEBUG_PORT)
    discovery_host = str(params.get("host") or "127.0.0.1")
    public_host = str(params.get("public_host") or "").strip() or None
    return _browser_debug_status_payload(port=port, discovery_host=discovery_host, public_host=public_host)


async def handle_browser_debug_launch(params: dict[str, Any]) -> dict[str, Any]:
    """Launch a Chromium-family browser with remote debugging enabled."""
    browser = str(params.get("browser") or "auto").strip().lower() or "auto"
    port = int(params.get("port") or DEFAULT_BROWSER_DEBUG_PORT)
    discovery_host = str(params.get("host") or "127.0.0.1")
    public_host = str(params.get("public_host") or "").strip() or None
    user_data_dir = str(params.get("user_data_dir") or "").strip()
    profile_directory = str(params.get("profile_directory") or "").strip()
    extra_args = params.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise ValueError("extra_args must be a list of strings")

    existing = _browser_debug_status_payload(port=port, discovery_host=discovery_host, public_host=public_host)
    if existing.get("listening"):
        existing.update({"launched": False, "already_listening": True})
        return existing

    executable = _find_browser_executable(browser)
    if not executable:
        raise FileNotFoundError(
            f"No Chromium-family browser executable found for '{browser}'. "
            "Install Chrome/Edge/Brave/Chromium or pass a supported browser name."
        )

    argv = [
        executable,
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if user_data_dir:
        argv.append(f"--user-data-dir={user_data_dir}")
    if profile_directory:
        argv.append(f"--profile-directory={profile_directory}")
    argv.extend(str(arg) for arg in extra_args)

    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_browser_detach_kwargs(),
    )

    for _ in range(20):
        await asyncio.sleep(0.5)
        status = _browser_debug_status_payload(port=port, discovery_host=discovery_host, public_host=public_host)
        if status.get("listening"):
            status.update({"launched": True, "already_listening": False, "argv": argv})
            return status

    status = _browser_debug_status_payload(port=port, discovery_host=discovery_host, public_host=public_host)
    status.update({
        "launched": True,
        "already_listening": False,
        "argv": argv,
        "hint": (
            "Browser process started but the debug endpoint is still unreachable. "
            "If your normal browser is already running, fully close it and retry, "
            "or launch with a separate user_data_dir."
        ),
    })
    return status


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
        # Ensure content_b64 is ASCII string before decoding
        if isinstance(content_b64, str):
            content_b64 = content_b64.encode('ascii')
        raw = base64.b64decode(content_b64)
        with open(path, "wb") as f:
            f.write(raw)
    else:
        text = params.get("text", "")
        with open(path, "w", encoding=encoding, newline=newline) as f:
            f.write(text)

    return {"path": str(path), "bytesWritten": path.stat().st_size}


@dataclass
class _NodeReadResult:
    content: str = ""
    total_lines: int = 0
    error: str | None = None


@dataclass
class _NodeWriteResult:
    bytes_written: int = 0
    error: str | None = None
    lsp_diagnostics: str | None = None


class _V4APatchFileOps:
    """Minimal file-ops adapter used by V4A patch parser/applicator."""

    def __init__(self, base_dir: str = ".") -> None:
        self.base_dir = Path(base_dir)

    def _resolve_path(self, path: str) -> Path:
        normalized = str(path)
        if os.path.isabs(normalized) or (len(normalized) >= 3 and normalized[1] == ":" and normalized[2] in ("\\", "/")):
            return Path(normalized)
        return self.base_dir / normalized

    def read_file_raw(self, path: str):
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return _NodeReadResult(error=f"File not found: {path}")

        if resolved.is_dir():
            return _NodeReadResult(error=f"Path is a directory: {path}")

        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = resolved.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = resolved.read_text(encoding="cp932")
                except Exception as exc:
                    return _NodeReadResult(error=f"Failed to read file: {exc}")

        return _NodeReadResult(content=_normalize_crlf(text), total_lines=max(1, len(text.splitlines()) or 1))

    def write_file(self, path: str, content: str):
        resolved = self._resolve_path(path)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            return _NodeWriteResult(bytes_written=len(content.encode("utf-8")))
        except Exception as exc:
            return _NodeWriteResult(error=f"Failed to write file: {exc}")

    def delete_file(self, path: str):
        resolved = self._resolve_path(path)
        try:
            if resolved.exists():
                resolved.unlink()
            return _NodeWriteResult(bytes_written=0)
        except Exception as exc:
            return _NodeWriteResult(error=f"Failed to delete file: {exc}")

    def move_file(self, src_path: str, dst_path: str):
        src = self._resolve_path(src_path)
        dst = self._resolve_path(dst_path)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
            return _NodeWriteResult(bytes_written=0)
        except Exception as exc:
            return _NodeWriteResult(error=f"Failed to move file: {exc}")


async def handle_file_patch(params: dict[str, Any]) -> dict[str, Any]:
    """Apply a V4A patch payload locally on the node."""
    patch = params.get("patch", "")
    if not isinstance(patch, str):
        return {"error": "Patch content must be a string"}

    if not patch.strip():
        return {"error": "Patch content is required"}

    try:
        try:
            from .patch_parser import parse_v4a_patch, apply_v4a_operations
        except ImportError:
            from patch_parser import parse_v4a_patch, apply_v4a_operations
    except Exception as exc:
        return {"error": f"Failed to load patch parser: {exc}"}

    operations, parse_error = parse_v4a_patch(patch)
    if parse_error:
        return {"error": parse_error}

    base_dir = params.get("base_dir", ".")
    file_ops = _V4APatchFileOps(base_dir=base_dir)
    try:
        patch_result = apply_v4a_operations(operations, file_ops)
        return patch_result.to_dict()
    except Exception as exc:
        return {"error": f"Patch apply failed: {exc}"}


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
        # Build raw PowerShell script; handle_terminal_exec will wrap it
        # with -EncodedCommand, so no manual escaping needed.
        cmd = (
            f"$cert = Get-ChildItem Cert:\\LocalMachine\\My | "
            f"Where-Object {{ $_.Subject -eq 'CN={subject}' }} | Select-Object -First 1; "
            f"Set-AuthenticodeSignature -FilePath '{file_path}' -Certificate $cert -HashAlgorithm {hash_alg}"
        )
    else:
        raise ValueError("thumbprint+signtoolPath or subject required")

    return await handle_terminal_exec({"cmd": cmd, "timeoutMs": 60000})


def _load_lsp_manager_factory():
    """Import the node-client LSP manager from package or script layouts."""
    try:
        from node_client.lsp import get_lsp_manager
        return get_lsp_manager
    except ModuleNotFoundError as exc:
        if exc.name != "node_client":
            raise

    script_dir = Path(__file__).resolve().parent
    import_attempts = [
        (script_dir.parent, "node_client.lsp"),  # repo checkout: <root>/node_client/hermes_node_client.py
        (script_dir, "lsp"),                    # copied script: hermes_node_client.py next to lsp/
    ]
    for sys_path_entry, module_name in import_attempts:
        entry = str(sys_path_entry)
        if entry not in sys.path:
            sys.path.insert(0, entry)
        try:
            module = __import__(module_name, fromlist=["get_lsp_manager"])
            return module.get_lsp_manager
        except ModuleNotFoundError as exc:
            missing_root = module_name.split(".", 1)[0]
            if exc.name != missing_root:
                raise

    # Last resort: load lsp/ as a standalone package.  The package name must not
    # include "node_client." unless the parent package exists in sys.modules;
    # otherwise relative imports inside lsp/__init__.py explode with
    # "No module named node_client".
    import importlib.util
    init_path = script_dir / "lsp" / "__init__.py"
    if not init_path.exists():
        init_path = script_dir.parent / "node_client" / "lsp" / "__init__.py"
    if not init_path.exists():
        raise ModuleNotFoundError("lsp package not found")

    spec = importlib.util.spec_from_file_location(
        "hermes_node_lsp",
        init_path,
        submodule_search_locations=[str(init_path.parent)],
    )
    if not spec or not spec.loader:
        raise ModuleNotFoundError("lsp package not found")
    lsp_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lsp_mod
    spec.loader.exec_module(lsp_mod)
    return lsp_mod.get_lsp_manager


async def handle_lsp(params: dict[str, Any]) -> dict[str, Any]:
    """LSP diagnostics via local language server.

    Expects params:
        action: "lint_after_write" | "get_diagnostics" | "shutdown"
        language: "csharp" | "python" | "typescript" | ...
        workspace_root: str (optional, defaults to cwd)
        file_path: str
        content: str (optional, for lint_after_write)
    """
    get_lsp_manager = _load_lsp_manager_factory()
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

    # Redirect stdout/stderr to log files so pythonw has valid file descriptors
    log_dir = Path.home() / ".hermes" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_log = open(log_dir / "node_client.log", "a")
    err_log = open(log_dir / "node_client.err", "a")

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
            stdout=out_log,
            stderr=err_log,
        )
    else:
        subprocess.Popen(
            args,
            start_new_session=True,
            close_fds=True,
            stdout=out_log,
            stderr=err_log,
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
    "file.patch": handle_file_patch,
    "file.delete": handle_file_delete,
    "file.list": handle_file_list,
    "search.content": handle_search_content,
    "search.files": handle_search_files,
    "msbuild": handle_msbuild,
    "signtool": handle_signtool,
    "node.restart": handle_node_restart,
    "lsp": handle_lsp,
    "browser.debug_status": handle_browser_debug_status,
    "browser.debug_launch": handle_browser_debug_launch,
}


def _load_optional_computer_use_handler() -> tuple[Optional[Callable[..., Any]], Optional[str]]:
    errors: list[str] = []
    if __package__:
        try:
            mod = importlib.import_module(".computer_use_handler", package=__package__)
            handler = getattr(mod, "handle_computer_use", None)
            if handler is not None:
                return handler, None
            errors.append(f"{__package__}.computer_use_handler missing handle_computer_use")
        except Exception as exc:
            errors.append(f"{__package__}.computer_use_handler: {exc}")
    try:
        mod = importlib.import_module("computer_use_handler")
        handler = getattr(mod, "handle_computer_use", None)
        if handler is not None:
            return handler, None
        errors.append("computer_use_handler missing handle_computer_use")
    except Exception as exc:
        errors.append(f"computer_use_handler: {exc}")
    return None, "; ".join(errors) if errors else "computer_use_handler unavailable"


def _caps_for_commands(commands: set[str]) -> list[str]:
    caps: set[str] = set()
    if any(cmd.startswith("terminal.") for cmd in commands):
        caps.add("terminal")
    if any(cmd.startswith("file.") or cmd.startswith("search.") for cmd in commands):
        caps.add("file")
    if any(cmd in {"msbuild", "signtool"} for cmd in commands):
        caps.add("build")
    if any(cmd.startswith("browser.") for cmd in commands):
        caps.add("browser")
    if "computer.use" in commands:
        caps.add("computer_use")
    if "lsp" in commands:
        caps.add("lsp")
    return sorted(caps)


def configure_runtime_features(allow_computer_use: bool) -> None:
    global COMMANDS, NODE_METADATA
    COMMANDS = set(BASE_COMMANDS) | set(BROWSER_COMMANDS)
    NODE_METADATA = {
        "browser_scope": "full_browser",
        "computer_use_enabled": False,
    }
    HANDLERS.pop("computer.use", None)

    if not allow_computer_use:
        return

    handler, error = _load_optional_computer_use_handler()
    if handler is None:
        logger.warning("[%s] --allow-computer-use requested but computer_use_handler is unavailable: %s", NODE_ID, error)
        return

    HANDLERS["computer.use"] = handler
    COMMANDS.add("computer.use")
    NODE_METADATA["computer_use_enabled"] = True


async def handle_invoke(ws: websockets.WebSocketClientProtocol, payload: dict[str, Any]) -> None:
    request_id = payload["id"]
    command = payload["command"]
    params = json.loads(payload.get("paramsJSON") or "{}")

    logger.info("[%s] Invoking %s", NODE_ID, command)
    try:
        if command not in COMMANDS:
            raise ValueError(f"Command not enabled for this node: {command}")
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
            "caps": _caps_for_commands(COMMANDS),
            "commands": sorted(COMMANDS),
            "metadata": NODE_METADATA,
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
            # Keepalive ping — respond with pong
            await ws.send(json.dumps({"type": "event", "event": "pong"}))
            continue


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
            async with websockets.connect(auth_url, max_size=10*1024*1024) as ws:
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


def build_arg_parser():
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Remote Node Client")
    parser.add_argument("--gateway", default=GATEWAY_URL, help="Deprecated: kept for compatibility, ignored")
    parser.add_argument("--api-server", default=API_SERVER_URL, help="API Server WebSocket URL (default: ws://localhost:8642/ws)")
    parser.add_argument("--token", default=NODE_TOKEN, help="Node authentication token")
    parser.add_argument("--node-id", default=NODE_ID, help="Unique node identifier")
    parser.add_argument(
        "--allow-computer-use",
        action="store_true",
        help="Enable the optional computer.use command if a local computer_use_handler is installed",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    configure_runtime_features(args.allow_computer_use)

    # Always use API Server URL
    api_server_url = args.api_server if args.api_server else args.gateway

    try:
        asyncio.run(connect_and_serve(args.gateway, args.token, args.node_id, api_server_url))
    except KeyboardInterrupt:
        logger.info("[%s] Shutting down.", args.node_id)


if __name__ == "__main__":
    main()
