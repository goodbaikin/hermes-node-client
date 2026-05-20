# Hermes Node Client with LSP RPC

分散 LSP アーキテクチャ — リモートノード上で言語サーバーを動かし、Hermes 本体から診断を取得する。

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────────────┐
│  N95 Proxmox LXC (lxc-207) — Hermes Gateway + AI Agent                  │
│  ┌─────────────────────┐    ┌──────────────────────────────────────┐   │
│  │  AIAgent Loop       │    │  LSP Manager (agent/lsp/manager.py)  │   │
│  │  ───────────────    │    │  ├─ Local LSP (Python, etc.)         │   │
│  │  write_file()       │───►│  ├─ Remote LSP Client                │   │
│  │  patch()            │    │  │   (agent/lsp/remote_client.py)    │   │
│  │  └─ lsp_check()     │◄───┤  └─ Result merge & format            │   │
│  └─────────────────────┘    └──────────────────────────────────────┘   │
│           │                                                             │
│           │ WebSocket / HTTP (既存 node_client プロトコル)              │
│           ▼                                                             │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  dev-win01 (Windows) — Node Client + LSP Server                 │   │
│  │  ┌─────────────────┐    ┌─────────────────────────────────────┐ │   │
│  │  │  hermes_node_   │    │  LSP RPC Server (lsp_server.py)     │ │   │
│  │  │  client.py      │◄──►│  ├─ OmniSharp (C#)                  │ │   │
│  │  │                 │    │  ├─ pyright (Python)                │ │   │
│  │  │  既存ツール:    │    │  ├─ tsserver (TypeScript)           │ │   │
│  │  │  terminal.exec  │    │  ├─ rust-analyzer (Rust)            │ │   │
│  │  │  file.read      │    │  ├─ gopls (Go)                      │ │   │
│  │  │  file.write     │    │  └─ Diagnostics cache               │ │   │
│  │  └─────────────────┘    └─────────────────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

## コンポーネント

| ファイル | 役割 |
|---|---|
| `hermes_node_client.py` | WebSocket クライアント。Gateway からツール呼び出しを受けて実行 |
| `lsp_server.py` | LSP 言語サーバーのライフサイクル管理。JSON-RPC over stdio |
| `agent/lsp/remote_client.py` | Hermes 側のリモート LSP クライアント。node_client 経由で診断取得 |

## フロー

### 1. ファイル書き込み時の診断

```
write_file("Program.cs", content)
    │
    ▼
LSPManager.enabled_for("Program.cs")
    │── ローカル LSP 不可（C# サーバー未インストール）
    │── リモート LSP 検索: node_id="dev-win01"
    ▼
RemoteLSPClient("dev-win01").lint_after_write(...)
    │
    ▼ HTTP/WebSocket
node_exec("dev-win01", {"tool": "lsp", "action": "lint_after_write", ...})
    │
    ▼
hermes_node_client.py ──► handle_lsp()
    │
    ▼
lsp_server.py ──► LSPServerManager
    │
    ▼
csharp-ls ──► textDocument/didOpen ──► 診断計算
    │
    ▼
textDocument/publishDiagnostics ──► キャッシュ
    │
    ▼
get_diagnostics() ──► 結果返却
    │
    ▼
RemoteLSPClient ──► 診断リストを会話に注入
```

### 2. 言語サーバー起動

```
lint_after_write(language="csharp", workspace_root="C:/.../COCONV.Deploy")
    │
    ▼
LSPSubprocess.start()
    │
    ├── バイナリ検索: csharp-ls
    │   ├── ~/.hermes/lsp/bin/csharp-ls
    │   ├── PATH
    │   └── %USERPROFILE%/.dotnet/tools
    │
    ├── プロセス起動: csharp-ls (stdio by default)
    │
    ├── initialize 送信
    │   {"rootUri": "file:///C:/.../COCONV.Deploy", ...}
    │
    └── initialized 受信 ──► 準備完了
```

## 対応言語サーバー

| 言語 | サーバー | インストール |
|---|---|---|
| Python | pyright | `npm install -g pyright` |
| C# | csharp-ls | `dotnet tool install --global csharp-ls` |
| TypeScript | typescript-language-server | `npm install -g typescript-language-server` |
| Rust | rust-analyzer | `rustup component add rust-analyzer` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |

## 設定

### node_client 側（dev-win01）

```powershell
# csharp-ls のインストール（.NET SDK 必須）
dotnet tool install --global csharp-ls

# または hermes lsp bin ディレクトリ
mkdir ~/.hermes/lsp/bin
# dotnet tools のパスを確認してコピー
# 通常: %USERPROFILE%\.dotnet\tools\csharp-ls.exe
```

### Hermes 側（config.yaml）

```yaml
lsp:
  enabled: true
  wait_mode: "document"
  wait_timeout: 5.0

# リモートノード設定
nodes:
  dev-win01:
    url: "ws://dev-win01:8642/ws"
    token: "your-token"
    lsp_enabled: true
```

## ワークスペース解決

LSP はワークスペース（プロジェクトルート）を必要とします。

```python
# lsp_server.py が自動解決
workspace_root = "C:/Users/goodb/workspace/COCONV.Deploy"

# マーカーファイル検索
for marker in [".sln", ".csproj"]:
    if (workspace_root / marker).exists():
        return workspace_root  # ✅ 有効

# マーカーがない場合、自動作成（最小スタブ）
(workspace_root / "pyproject.toml").write_text("[build-system]\n")
```

## 診断フォーマット

```python
[
  {
    "range": {
      "start": {"line": 15, "character": 6},
      "end": {"line": 15, "character": 11}
    },
    "severity": 1,  # 1=Error, 2=Warning, 3=Info, 4=Hint
    "message": "The name 'logger' does not exist in the current context",
    "source": "OmniSharp",
    "code": "CS0103"
  }
]
```

Hermes 会話への注入例:
```
⚠️ 3 diagnostic(s) in Program.cs:
  [Error] Line 16:6 (OmniSharp) [CS0103]: The name 'logger' does not exist...
  [Warning] Line 23:4 (OmniSharp) [CS0168]: The variable 'result' is declared...
```

## テスト

```bash
# ローカルテスト（lxc-207）
cd /tmp
python3 -c "
import asyncio
import sys
sys.path.insert(0, '/home/goodbaikin/.hermes/hermes-agent/node_client')
from lsp_server import get_lsp_manager

async def test():
    mgr = get_lsp_manager()
    result = await mgr.handle_request({
        'action': 'lint_after_write',
        'language': 'python',
        'workspace_root': '/tmp/test_ws',
        'file_path': '/tmp/test_ws/test.py',
        'content': 'x = 1\ny = \"hello\"\nprint(x + y)\n',
    })
    print(result)
    await mgr.shutdown_all()

asyncio.run(test())
"
```

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `lsp_server.py not found` | import パス問題 | `node_client/` を PYTHONPATH に追加 |
| `Failed to start LSP for python` | pyright 未インストール | `npm install -g pyright` |
| 診断が空 | マーカーファイル不足 | 自動作成されるはず。手動で `pyproject.toml` 作成 |
| タイムアウト | 言語サーバー起動遅延 | `wait_timeout` を増やす |
| `csharp-ls not found` | .NET SDK 未インストール | `dotnet tool install --global csharp-ls` |

## 今後の拡張

- [ ] C++ (clangd)
- [ ] Java (jdtls)
- [ ] Kotlin (kotlin-language-server)
- [ ] Zig (zls)
- [ ] 複数ノード同時利用（負荷分散）
