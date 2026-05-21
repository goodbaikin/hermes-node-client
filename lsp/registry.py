"""LSP server registry — command lines and root markers per language."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class LanguageConfig:
    bin: str
    args: List[str]
    root_markers: List[str]
    wait_seconds: float = 5.0
    skip_did_change: bool = False
    # Optional: custom handler module path under languages/
    handler_module: Optional[str] = None


LSP_REGISTRY: Dict[str, LanguageConfig] = {
    "csharp": LanguageConfig(
        bin="roslyn-language-server",
        args=["--stdio", "--autoLoadProjects"],
        root_markers=[".sln", ".csproj"],
        wait_seconds=10.0,
        skip_did_change=True,
        handler_module="csharp",
    ),
    "python": LanguageConfig(
        bin="pyright-langserver",
        args=["--stdio"],
        root_markers=["pyproject.toml", "setup.py", "setup.cfg"],
        wait_seconds=3.0,
    ),
    "typescript": LanguageConfig(
        bin="typescript-language-server",
        args=["--stdio"],
        root_markers=["package.json", "tsconfig.json"],
        wait_seconds=3.0,
    ),
    "javascript": LanguageConfig(
        bin="typescript-language-server",
        args=["--stdio"],
        root_markers=["package.json"],
        wait_seconds=3.0,
    ),
    "rust": LanguageConfig(
        bin="rust-analyzer",
        args=[],
        root_markers=["Cargo.toml"],
        wait_seconds=5.0,
    ),
    "go": LanguageConfig(
        bin="gopls",
        args=[],
        root_markers=["go.mod"],
        wait_seconds=3.0,
    ),
    "powershell": LanguageConfig(
        bin="",
        args=[],
        root_markers=[".ps1", ".psm1", ".psd1"],
        wait_seconds=5.0,
        handler_module="powershell",
    ),
    "html": LanguageConfig(
        bin="vscode-html-language-server",
        args=["--stdio"],
        root_markers=["package.json", ".html"],
        wait_seconds=3.0,
        handler_module="web",
    ),
    "css": LanguageConfig(
        bin="vscode-css-language-server",
        args=["--stdio"],
        root_markers=["package.json", ".css"],
        wait_seconds=3.0,
        handler_module="web",
    ),
}


def get_config(lang: str) -> Optional[LanguageConfig]:
    return LSP_REGISTRY.get(lang)
