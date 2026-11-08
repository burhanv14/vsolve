"""Unit tests for GitHub issue URL parsing."""

import pytest

from issue_agent.github_client import GitHubError, parse_issue_url


def test_valid_url():
    p = parse_issue_url("https://github.com/owner/repo/issues/42")
    assert p.owner == "owner"
    assert p.repo == "repo"
    assert p.number == 42
    assert p.html_url == "https://github.com/owner/repo/issues/42"


def test_valid_url_with_trailing_slash():
    p = parse_issue_url("https://github.com/owner/repo/issues/7/")
    assert p.number == 7


def test_invalid_urls():
    for bad in [
        "https://github.com/owner/repo/pull/42",
        "https://gitlab.com/owner/repo/issues/42",
        "https://github.com/owner/repo/issues/notanumber",
        "https://github.com/owner/repo/issues/",
        "not a url",
        "http://github.com/owner/repo/issues/42",
        "",
    ]:
        with pytest.raises(GitHubError):
            parse_issue_url(bad)
