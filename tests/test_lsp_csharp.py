#!/usr/bin/env python3
"""
Integration test for node_client C# LSP (Roslyn Language Server).

Usage:
    python test_lsp_csharp.py [test_name] [node_id] [workspace_root] [test_file]

Args:
    test_name       - all, basic, clean, delta, get_diagnostics, direct (default: all)
    node_id         - Hermes node ID (default: dev-win01)
    workspace_root  - Workspace root path (default: C:/Users/goodb/workspace/LspTest)
    test_file       - Test file path (default: C:/Users/goodb/workspace/LspTest/Program.cs)

Environment:
    HERMES_NODE_ID         - Overrides node_id argument
    HERMES_CSHARP_WORKSPACE - Overrides workspace_root argument
    HERMES_CSHARP_TESTFILE  - Overrides test_file argument
"""

import json
import os
import sys
import time
from pathlib import Path

# Add hermes-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment
from tools.node_invoke import node_invoke


# ---------------------------------------------------------------------------
# Configuration — args override env, env overrides defaults
# ---------------------------------------------------------------------------
def _get_config():
    defaults = {
        "node_id": "dev-win01",
        "workspace_root": "C:/Users/goodb/workspace/LspTest",
        "test_file": "C:/Users/goodb/workspace/LspTest/Program.cs",
    }

    # Env overrides defaults
    defaults["node_id"] = os.environ.get("HERMES_NODE_ID", defaults["node_id"])
    defaults["workspace_root"] = os.environ.get(
        "HERMES_CSHARP_WORKSPACE", defaults["workspace_root"]
    )
    defaults["test_file"] = os.environ.get(
        "HERMES_CSHARP_TESTFILE", defaults["test_file"]
    )

    # Args override env
    if len(sys.argv) > 2:
        defaults["node_id"] = sys.argv[2]
    if len(sys.argv) > 3:
        defaults["workspace_root"] = sys.argv[3]
    if len(sys.argv) > 4:
        defaults["test_file"] = sys.argv[4]

    return defaults


CFG = _get_config()
NODE_ID = CFG["node_id"]
WORKSPACE_ROOT = CFG["workspace_root"]
TEST_FILE = CFG["test_file"]

# Test file contents
CLEAN_CONTENT = """using System;

namespace LspTest
{
    class Program
    {
        static void Main()
        {
            int x = 42;
            Console.WriteLine(x);
        }
    }
}
"""

ERROR_CONTENT = """using System;

namespace LspTest
{
    class Program
    {
        static void Main()
        {
            int x = "hello";
            Console.WriteLine(x);
        }
    }
}
"""

NEW_ERROR_CONTENT = """using System;

namespace LspTest
{
    class Program
    {
        static void Main()
        {
            int x = 42;
            int y = "hello";
            Console.WriteLine(x);
        }
    }
}
"""


def setup_file_operations():
    """Create and configure ShellFileOperations for remote LSP."""
    env = LocalEnvironment()
    fo = ShellFileOperations(env)
    fo._node_id = NODE_ID
    fo.cwd = WORKSPACE_ROOT
    return fo


def print_diagnostics(diags, label="Diagnostics"):
    """Pretty-print diagnostics."""
    print(f"  {label}: {len(diags)} item(s)")
    for d in diags:
        code = d.get("code", "N/A")
        line = d.get("range", {}).get("start", {}).get("line", 0)
        msg = d.get("message", "")[:60]
        severity = {1: "ERROR", 2: "WARN", 3: "INFO", 4: "HINT"}.get(
            d.get("severity", 1), "?"
        )
        print(f"    [{severity}] Line {line}: {code}: {msg}")


def test_basic_lint():
    """Test basic lint_after_write with error content."""
    print("\n=== Test: Basic lint_after_write ===")
    fo = setup_file_operations()

    diags = fo._node_lsp_lint_after_write(TEST_FILE, ERROR_CONTENT, delta=False)
    print_diagnostics(diags)

    assert len(diags) > 0, "Expected at least 1 diagnostic for error content"
    assert any(d.get("code") == "CS0029" for d in diags), "Expected CS0029 (type mismatch)"
    print("  PASSED")
    return True


def test_clean_file():
    """Test lint_after_write with clean content."""
    print("\n=== Test: Clean file lint_after_write ===")
    fo = setup_file_operations()

    # Ensure file has clean content first (write directly to disk)
    import base64

    clean_content_b64 = base64.b64encode(CLEAN_CONTENT.encode("utf-8")).decode("utf-8")
    node_invoke(
        NODE_ID,
        "file.write",
        {"path": TEST_FILE, "content": clean_content_b64},
        timeout_ms=30000,
    )
    time.sleep(2)

    # Request lint to refresh diagnostics
    diags = fo._node_lsp_lint_after_write(TEST_FILE, CLEAN_CONTENT, delta=False)
    print_diagnostics(diags)

    # Clean file may have info-level diagnostics (unused vars, etc.)
    # but should not have errors
    errors = [d for d in diags if d.get("severity") == 1]
    assert len(errors) == 0, f"Expected 0 errors for clean content, got {len(errors)}"
    print("  PASSED")
    return True


def test_delta_filtering():
    """Test delta filtering: baseline -> edit -> delta."""
    print("\n=== Test: Delta filtering ===")
    fo = setup_file_operations()

    # Step 0: Write clean content to file via file.write
    print("  Step 0: Write clean content to file")
    import base64

    clean_content_b64 = base64.b64encode(CLEAN_CONTENT.encode("utf-8")).decode("utf-8")
    result0 = node_invoke(
        NODE_ID,
        "file.write",
        {"path": TEST_FILE, "content": clean_content_b64},
        timeout_ms=30000,
    )
    print(f"  Step 0 result: {result0}")
    time.sleep(1)

    # Step 0.5: Ensure clean diagnostics by running lint_after_write first
    print("  Step 0.5: Refresh diagnostics with clean content")
    fo._node_lsp_lint_after_write(TEST_FILE, CLEAN_CONTENT, delta=False)
    time.sleep(1)

    # Step 1: Baseline with clean content
    print("  Step 1: snapshot_baseline (clean)")
    raw_baseline = node_invoke(
        NODE_ID,
        "lsp",
        {
            "action": "snapshot_baseline",
            "language": "csharp",
            "file_path": TEST_FILE,
            "workspace_root": WORKSPACE_ROOT,
        },
        timeout_ms=60000,
    )
    print(f"  Raw baseline: {raw_baseline}")
    baseline = json.loads(raw_baseline).get("payload", {})
    print(f"  Baseline captured: {baseline.get('baseline_captured')}")

    # Step 2: Introduce NEW error and get delta
    print("  Step 2: lint_after_write with NEW error")
    diags = fo._node_lsp_lint_after_write(TEST_FILE, ERROR_CONTENT, delta=True)
    print(f"  Delta (new error): {len(diags)} item(s)")
    for d in diags:
        print(f"    - {d.get('code')} {d.get('message', '')[:60]}")

    assert len(diags) > 0, "Expected delta diagnostics for new error"

    # Step 3: Same error again should return 0 (already in baseline)
    print("  Step 3: lint_after_write again (same error)")
    diags2 = fo._node_lsp_lint_after_write(TEST_FILE, ERROR_CONTENT, delta=True)
    print(f"  Delta (same error, baseline NOT auto-updated): {len(diags2)} item(s)")

    # Baseline is NOT auto-updated by lint_after_write, so same error still appears as delta
    assert len(diags2) == 1, "Expected 1 delta since baseline was not auto-updated"

    # Step 4: Update baseline with current error state
    print("  Step 4: snapshot_baseline with error content")
    raw_baseline2 = node_invoke(
        NODE_ID,
        "lsp",
        {
            "action": "snapshot_baseline",
            "language": "csharp",
            "file_path": TEST_FILE,
            "workspace_root": WORKSPACE_ROOT,
        },
        timeout_ms=60000,
    )
    print(f"  Raw baseline2: {raw_baseline2}")

    # Step 5: Same error again should now return 0
    print("  Step 5: lint_after_write again after baseline update")
    diags3 = fo._node_lsp_lint_after_write(TEST_FILE, ERROR_CONTENT, delta=True)
    print(f"  Delta (same error after baseline update): {len(diags3)} item(s)")

    assert len(diags3) == 0, "Expected 0 delta after baseline was updated"

    print("  ✓ Delta filtering works correctly")
    return True


def test_get_diagnostics():
    """Test get_diagnostics endpoint."""
    print("\n=== Test: get_diagnostics ===")
    fo = setup_file_operations()

    # First write error content
    fo._node_lsp_lint_after_write(TEST_FILE, ERROR_CONTENT, delta=False)

    # Then get diagnostics
    diags = fo._node_lsp_get_diagnostics(TEST_FILE, delta=False)
    print_diagnostics(diags)

    assert len(diags) > 0, "Expected diagnostics from get_diagnostics"
    print("  PASSED")
    return True


def test_direct_node_invoke():
    """Test direct node_invoke call (bypass file_operations)."""
    print("\n=== Test: Direct node_invoke ===")

    result = node_invoke(
        NODE_ID,
        "lsp",
        {
            "action": "lint_after_write",
            "language": "csharp",
            "workspace_root": WORKSPACE_ROOT,
            "file_path": TEST_FILE,
            "content": ERROR_CONTENT,
        },
        timeout_ms=60000,
    )

    parsed = json.loads(result)
    payload = parsed.get("payload", {})
    diags = payload.get("diagnostics", []) if isinstance(payload, dict) else []

    print_diagnostics(diags)
    assert len(diags) > 0, "Expected diagnostics from direct node_invoke"
    print("  PASSED")
    return True


def run_all_tests():
    """Run all tests and report results."""
    # Run clean test first to avoid contamination from error content
    tests = {
        "clean": test_clean_file,
        "basic": test_basic_lint,
        "delta": test_delta_filtering,
        "get_diagnostics": test_get_diagnostics,
        "direct": test_direct_node_invoke,
    }

    results = {}
    start_time = time.time()

    for name, test_fn in tests.items():
        try:
            results[name] = test_fn()
        except AssertionError as e:
            print(f"  FAILED: {e}")
            results[name] = False
        except Exception as e:
            print(f"  ERROR: {e}")
            results[name] = False

    elapsed = time.time() - start_time

    print(f"\n{'='*50}")
    print(f"Results ({elapsed:.1f}s):")
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")

    total = len(results)
    passed = sum(results.values())
    print(f"\nTotal: {passed}/{total} passed")

    return all(results.values())


if __name__ == "__main__":
    test_name = sys.argv[1] if len(sys.argv) > 1 else "all"

    print(f"Configuration:")
    print(f"  NODE_ID: {NODE_ID}")
    print(f"  WORKSPACE_ROOT: {WORKSPACE_ROOT}")
    print(f"  TEST_FILE: {TEST_FILE}")

    if test_name == "all":
        success = run_all_tests()
    elif test_name == "basic":
        success = test_basic_lint()
    elif test_name == "clean":
        success = test_clean_file()
    elif test_name == "delta":
        success = test_delta_filtering()
    elif test_name == "get_diagnostics":
        success = test_get_diagnostics()
    elif test_name == "direct":
        success = test_direct_node_invoke()
    else:
        print(f"Unknown test: {test_name}")
        print(f"Available: all, basic, clean, delta, get_diagnostics, direct")
        sys.exit(1)

    sys.exit(0 if success else 1)
