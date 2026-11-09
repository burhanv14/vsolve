"""Language + test/lint/format command detection.

Inspects a workspace tree to guess the primary language and suggest common
commands. Pure heuristics; no network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Language -> (filenames, extensions)
LANGUAGE_SIGNALS = {
    "Python": ({"pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "Pipfile"}, {".py"}),
    "JavaScript": ({"package.json"}, {".js", ".jsx", ".mjs"}),
    "TypeScript": ({"tsconfig.json", "package.json"}, {".ts", ".tsx"}),
    "Go": ({"go.mod", "go.sum"}, {".go"}),
    "Rust": ({"Cargo.toml", "Cargo.lock"}, {".rs"}),
    "Java": ({"pom.xml", "build.gradle", "build.gradle.kts"}, {".java"}),
    "Ruby": ({"Gemfile", "Rakefile"}, {".rb"}),
    "PHP": ({"composer.json"}, {".php"}),
    "C#": ({"*.csproj", "*.sln"}, {".cs"}),
    "C++": ({"CMakeLists.txt", "Makefile", "meson.build"}, {".cpp", ".cc", ".cxx", ".hpp", ".h"}),
    "C": ({"Makefile", "CMakeLists.txt"}, {".c", ".h"}),
}

# Language -> common commands (test, lint, format)
COMMANDS = {
    "Python": {
        "test": "pytest -q",
        "lint": "ruff check .",
        "format": "ruff format --check .",
    },
    "JavaScript": {
        "test": "npm test",
        "lint": "npx eslint .",
        "format": "npx prettier --check .",
    },
    "TypeScript": {
        "test": "npm test",
        "lint": "npx eslint .",
        "format": "npx prettier --check .",
    },
    "Go": {
        "test": "go test ./...",
        "lint": "go vet ./...",
        "format": "gofmt -l .",
    },
    "Rust": {
        "test": "cargo test",
        "lint": "cargo clippy --all-targets",
        "format": "cargo fmt --check",
    },
    "Java": {
        "test": "mvn test -q",
        "lint": "",
        "format": "",
    },
    "Ruby": {
        "test": "bundle exec rake test",
        "lint": "rubocop",
        "format": "rubocop -a --dry-run",
    },
    "PHP": {
        "test": "vendor/bin/phpunit",
        "lint": "",
        "format": "",
    },
    "C#": {
        "test": "dotnet test",
        "lint": "",
        "format": "dotnet format --verify-no-changes",
    },
    "C++": {
        "test": "",
        "lint": "",
        "format": "clang-format --dry-run --Werror",
    },
    "C": {
        "test": "",
        "lint": "",
        "format": "",
    },
}


@dataclass
class DetectionResult:
    language: str
    test_command: Optional[str]
    lint_command: Optional[str]
    format_command: Optional[str]
    signals: list


def detect_language(workspace_path: str) -> DetectionResult:
    root = Path(workspace_path)
    scores: dict = {}
    signals_found: list = []

    print(f"[issue_agent] [detector] Scanning workspace for language signals: {workspace_path}", flush=True)

    # Scan top-level files and extensions (bounded walk).
    for dirpath, dirnames, filenames in os.walk(root):
        # prune heavy dirs
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {".git", "node_modules", ".venv", "venv", "target", "build", "dist"}
        ]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(root).as_posix()
            # only consider top two levels for signals
            if rel.count("/") > 2:
                continue
            for lang, (files, exts) in LANGUAGE_SIGNALS.items():
                if fname in files:
                    scores[lang] = scores.get(lang, 0) + 3
                    signals_found.append(f"{lang}:{fname}")
                if fpath.suffix in exts:
                    scores[lang] = scores.get(lang, 0) + 1

    if not scores:
        print(f"[issue_agent] [detector] No language signals found; returning 'unknown'", flush=True)
        return DetectionResult(
            language="unknown",
            test_command=None,
            lint_command=None,
            format_command=None,
            signals=signals_found,
        )

    language = max(scores, key=scores.get)
    cmds = COMMANDS.get(language, {})
    print(f"[issue_agent] [detector] Detected language: {language} (signals: {signals_found})", flush=True)
    print(f"[issue_agent] [detector]   test={cmds.get('test') or None}, lint={cmds.get('lint') or None}, format={cmds.get('format') or None}", flush=True)
    return DetectionResult(
        language=language,
        test_command=cmds.get("test") or None,
        lint_command=cmds.get("lint") or None,
        format_command=cmds.get("format") or None,
        signals=signals_found,
    )
