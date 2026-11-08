"""Unit tests for language/command detection (no network)."""

import tempfile
from pathlib import Path

from issue_agent.detector import detect_language


def test_python_detected():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "pyproject.toml").write_text("")
        (Path(d) / "src").mkdir(parents=True, exist_ok=True)
        Path(d, "src", "a.py").write_text("print(1)\n")
        res = detect_language(d)
        assert res.language == "Python"
        assert res.test_command == "pytest -q"
        assert res.lint_command == "ruff check ."


def test_unknown_when_no_signals():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "README.md").write_text("hi\n")
        res = detect_language(d)
        assert res.language == "unknown"
        assert res.test_command is None
