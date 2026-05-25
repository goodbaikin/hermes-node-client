import subprocess
import sys
from pathlib import Path


def test_handle_lsp_imports_when_node_client_script_runs_from_own_directory():
    repo_root = Path(__file__).resolve().parents[2]
    node_client_dir = repo_root / "node_client"
    code = r'''
import asyncio
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("hermes_node_client_under_test", Path("hermes_node_client.py").resolve())
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

async def main():
    result = await mod.handle_lsp({
        "action": "shutdown",
        "language": "python",
        "workspace_root": str(Path.cwd()),
        "file_path": str(Path.cwd() / "x.py"),
    })
    assert result == {"status": "ok"}, result

asyncio.run(main())
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=node_client_dir,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
