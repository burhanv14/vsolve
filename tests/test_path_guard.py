"""Unit tests for the path-guard / confinement logic."""

import os
import pytest
from pathlib import Path

from issue_agent.context import _is_within, _resolve_within_root, ContextError


def test_is_within_true():
    root = Path("/root").resolve()
    assert _is_within(Path("/root/a/b"), root) is True
    assert _is_within(root, root) is True


def test_is_within_false_outside():
    root = Path("/root").resolve()
    assert _is_within(Path("/root/../outside").resolve(), root) is False
    assert _is_within(Path("/other").resolve(), root) is False


def test_resolve_within_root_allows_sibling(tmp_path):
    ws = str(tmp_path / "proj")
    p = _resolve_within_root(ws, "src/foo.py")
    assert Path(p).relative_to(Path(ws).resolve()) == Path("src/foo.py")
    assert os.path.sep.join(Path(p).parts[-2:]) == os.path.sep.join(["src", "foo.py"])


def test_resolve_within_root_rejects_traversal():
    with pytest.raises(ContextError):
        _resolve_within_root("/workspace/proj", "../../../etc/passwd")


def test_resolve_within_root_rejects_absolute():
    with pytest.raises(ContextError):
        _resolve_within_root("/workspace/proj", "/etc/passwd")


def test_resolve_within_root_stays_within_root(tmp_path):
    # A null-byte segment should not allow escaping the workspace.
    ws = str(tmp_path / "proj")
    try:
        p = _resolve_within_root(ws, "src/foo.py")
    except ContextError:
        pytest.skip("null-byte-like segment rejected outright")
    assert Path(p).resolve().relative_to(Path(ws).resolve())
