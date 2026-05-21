"""Test Python LSP (Pyright) on dev-win01 via node_invoke."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools.node_invoke import node_invoke


def test_python_lsp():
    """Test Pyright LSP on dev-win01."""
    print("\n=== Test: Python LSP (Pyright) on dev-win01 ===")

    workspace = "C:/Users/goodb/workspace/test_pyright"
    file_path = f"{workspace}/test.py"

    # Create workspace and test file
    node_invoke("dev-win01", "terminal.exec", {
        "command": f"mkdir -Force {workspace}"
    })

    # Write a Python file with a type error
    content = "def greet(name: str) -> int:\n    return 'hello' + name\n"
    node_invoke("dev-win01", "file.write", {
        "path": file_path,
        "content": content,
    })

    # Initialize LSP
    print("Initializing LSP...")
    result = node_invoke("dev-win01", "lsp", {
        "action": "lint_after_write",
        "language": "python",
        "workspace_root": workspace,
        "file_path": file_path,
        "content": content,
    })
    print(f"Result: {result}")

    try:
        data = json.loads(result)
        if data.get("ok"):
            payload = data.get("payload", {})
            diags = payload.get("diagnostics", [])
            print(f"Diagnostics: {len(diags)} items")
            for d in diags:
                print(f"  [{d.get('severity', '?')}] {d.get('code')}: {d.get('message', '')[:80]}")
            return len(diags) > 0
        else:
            print(f"Error: {data.get('error')}")
            return False
    except Exception as e:
        print(f"Parse error: {e}")
        return False


if __name__ == "__main__":
    ok = test_python_lsp()
    print(f"\n{'PASS' if ok else 'FAIL'}: Python LSP test")
    sys.exit(0 if ok else 1)
