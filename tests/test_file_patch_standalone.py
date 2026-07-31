import os
import subprocess
import sys
import textwrap
from pathlib import Path


NODE_CLIENT_DIR = Path(__file__).resolve().parents[1]


def _run_node_client_patch_script(script: str, tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(NODE_CLIENT_DIR)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_file_patch_add_works_without_hermes_tools_package(tmp_path):
    completed = _run_node_client_patch_script(
        """
        import asyncio
        import json
        import pathlib
        import tempfile

        import hermes_node_client as client

        base_dir = pathlib.Path(tempfile.mkdtemp())
        patch = "*** Begin Patch\\n*** Add File: nested/hello.txt\\n+hello from node\\n*** End Patch\\n"
        result = asyncio.run(client.handle_file_patch({"patch": patch, "base_dir": str(base_dir)}))
        created = base_dir / "nested" / "hello.txt"
        content = created.read_text(encoding="utf-8") if created.exists() else None
        print(json.dumps({"result": result, "content": content}))
        """,
        tmp_path,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert '"success": true' in completed.stdout
    assert '"content": "hello from node"' in completed.stdout


def test_file_patch_update_works_without_hermes_tools_package(tmp_path):
    completed = _run_node_client_patch_script(
        """
        import asyncio
        import json
        import pathlib
        import tempfile

        import hermes_node_client as client

        base_dir = pathlib.Path(tempfile.mkdtemp())
        target = base_dir / "hello.txt"
        target.write_text("one\\ntwo\\nthree\\n", encoding="utf-8")
        patch = "*** Begin Patch\\n*** Update File: hello.txt\\n@@\\n one\\n-two\\n+TWO\\n three\\n*** End Patch\\n"
        result = asyncio.run(client.handle_file_patch({"patch": patch, "base_dir": str(base_dir)}))
        print(json.dumps({"result": result, "content": target.read_text(encoding="utf-8")}))
        """,
        tmp_path,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert '"success": true' in completed.stdout
    assert '"content": "one\\nTWO\\nthree\\n"' in completed.stdout


def test_file_write_creates_parent_and_reports_read_metadata(tmp_path):
    completed = _run_node_client_patch_script(
        """
        import asyncio
        import base64
        import json
        import pathlib
        import tempfile

        import hermes_node_client as client

        target = pathlib.Path(tempfile.mkdtemp()) / "nested" / "hello.txt"
        encoded = base64.b64encode(b"one\\r\\ntwo\\r\\n").decode("ascii")
        write_result = asyncio.run(client.handle_file_write({"path": str(target), "content": encoded}))
        read_result = asyncio.run(client.handle_file_read({"path": str(target), "encoding": "utf-8"}))
        empty = target.parent / "empty.txt"
        empty_result = asyncio.run(client.handle_file_write({"path": str(empty), "content": ""}))
        print(json.dumps({"write": write_result, "read": read_result, "empty": empty_result}))
        """,
        tmp_path,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert '"dirsCreated": true' in completed.stdout
    assert '"lineEnding": "crlf"' in completed.stdout
    assert '"fileSize": 10' in completed.stdout
    assert '"empty": {"path":' in completed.stdout
    assert '"bytesWritten": 0' in completed.stdout
