# issue_agent

A local MCP server (FastMCP, Python 3.11+) that helps an LLM turn a GitHub
issue into an open-source pull request.

The host LLM does the reasoning and code editing. This server provides the
deterministic git/GitHub tools, guardrails, and a per-workspace workflow state
machine, exposed over **streamable HTTP** bound to `127.0.0.1` only.

## Layout

```
issue_agent/
├── pyproject.toml
├── README.md
├── .env.example
├── issue_agent/              (Python package)
│   ├── __init__.py
│   ├── server.py             # FastMCP app + tool definitions
│   ├── config.py             # pydantic-settings
│   ├── github_client.py      # issues, comments, forks, PRs, rate limits
│   ├── repo_manager.py       # clone, branch, diff, commit, push, cleanup
│   ├── analyzer.py           # issue actionability verdict
│   ├── detector.py           # detect language and test/lint commands
│   ├── sandbox.py            # run commands: timeout, output truncation, scrubbed env
│   ├── context.py            # tree, ripgrep search, file slices
│   ├── state.py              # per-session workflow state machine
│   └── models.py             # pydantic models for tool I/O
└── tests/
```

## Stack

- `fastmcp` — MCP server framework (streamable HTTP)
- `PyGithub` — GitHub REST API access
- `GitPython` — local git operations
- `pydantic` / `pydantic-settings` — typed config and tool I/O
- `python-dotenv` — load `.env`
- `pytest` — unit tests
- Managed with `uv`

## Setup

```bash
# Create the project (if starting fresh)
uv init issue_agent
cd issue_agent

# Copy this repo's files into place, then:
cp .env.example .env
# Edit .env and set GITHUB_TOKEN (see Token scopes below).

# Sync dependencies and create the .venv
uv sync

# Run the MCP server (streamable HTTP on http://127.0.0.1:8765/mcp)
uv run issue-agent
```

The console script entry point is `issue-agent = "issue_agent.server:main"`.

### Token scopes

Create a GitHub personal access token (classic or fine-grained) and put it in
`.env`:

```
GITHUB_TOKEN=ghp_...
```

Required scopes depend on what you want to do:

| Operation | Needed scope |
|-----------|--------------|
| Read issues / comments / labels / PRs | `public_repo` (public) or `repo` (private) |
| Fork a repo | `repo` / `public_repo` |
| Open a PR from your fork | `public_repo` / `repo` |

The token is read **only from the environment**, is **never logged or
returned** to the host LLM, and is **scrubbed from the environment of any
sandboxed command**. Never commit `.env`.

## Transport

The server binds to `127.0.0.1` only (never `0.0.0.0`) over streamable HTTP:

```
http://127.0.0.1:8765/mcp
```

`HOST`, `PORT`, and `PATH` are configurable via environment variables
(defaults `127.0.0.1:8765/mcp`):

```bash
HOST=127.0.0.1 PORT=8765 PATH=/mcp uv run issue-agent
```

The server is started with `mcp.run(transport="http", host=HOST, port=PORT, path="/mcp")`.

## Client configuration

### Cursor

Add to your Cursor MCP settings (Settings → Features → MCP):

```json
{
  "mcpServers": {
    "issue_agent": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

### OpenCode

Add to your OpenCode MCP config:

```json
{
  "mcpServers": {
    "issue_agent": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

## MCP tools

All tools return structured Pydantic models and validate the workflow state
before acting.

| Tool | Purpose | Required state |
|------|---------|----------------|
| `analyze_issue(url)` | Fetch issue + related data; return actionability verdict | `NEW` → `ANALYZED` |
| `prepare_workspace(url)` | Fork, clone, sync, branch | `ANALYZED` + actionable → `PREPARED` |
| `get_context(query, paths)` | ripgrep search or file slices (workspace-only) | any |
| `apply_patch(diff)` | Apply a unified diff (resets to `PREPARED`) | `PREPARED`/`CHECKS_PASSED` |
| `run_checks(command)` | Sandboxed test/lint/format run | `PREPARED` → `CHECKS_PASSED` |
| `commit_and_push(message)` | Commit + push to fork only (elicitation confirm) | `CHECKS_PASSED` → `PUSHED` |
| `create_pr(title, body)` | Open PR fork → upstream (elicitation confirm) | `PUSHED` → `PR_OPENED` |
| `get_status()` | Current state, branch, diff summary | any |
| `cleanup_workspace()` | Delete the workspace | any |

State machine:

```
NEW → ANALYZED → PREPARED → CHECKS_PASSED → PUSHED → PR_OPENED
```

Editing files after `CHECKS_PASSED` resets state to `PREPARED`.

## Typical workflow

```text
1. analyze_issue("https://github.com/owner/repo/issues/42")
   → verdict: actionable, risk, reasons, contributing notes
2. prepare_workspace("https://github.com/owner/repo/issues/42")
   → forks (reuses existing fork), clones to
     WORKSPACE_ROOT/owner__repo__42, adds upstream remote, syncs,
     creates branch fix/42-<slug>, detects language + commands
3. [host LLM edits files, or apply_patch(diff)]
4. run_checks()   → runs pytest / ruff / etc. in sandbox
   → all pass → state CHECKS_PASSED
5. commit_and_push("fix: resolve issue #42")
   → elicitation shows diff stat; confirm; push to fork only
   → state PUSHED
6. create_pr("fix: resolve issue #42")
   → elicitation confirms; opens PR fork→upstream with Fixes #42 body
   → state PR_OPENED, returns PR URL
7. cleanup_workspace()  (optional) deletes the workspace
```

## Safety model

- **Loopback-only binding**: server binds to `127.0.0.1`; `0.0.0.0` is
  rejected by config validation.
- **Token handling**: `GITHUB_TOKEN` is read from env only, never logged,
  never returned, scrubbed from sandboxed command environments.
- **No force-push**: pushes to `origin` (your fork) only, never `upstream`,
  never with `--force`.
- **Workspace confinement**: all file/git/sandbox paths are resolved and
  checked to stay inside `WORKSPACE_ROOT`; path traversal and escaping
  symlinks are rejected.
- **State enforcement**: every tool validates the required workflow state and
  returns a clear error otherwise.
- **Refusal conditions**: the server refuses when the issue is claimed
  (assignee), already has a linked/open PR referencing it, has `wontfix`/
  `duplicate`/closed labels, is locked, or when `CONTRIBUTING.md` forbids AI
  contributions.
- **Diff limit**: commits are rejected if they exceed `MAX_DIFF_LINES`.
- **Structured logging**: to `stderr`.
- **Elicitation**: `commit_and_push` and `create_pr` ask the user for
  confirmation via MCP elicitation, showing the diff stat / PR preview.

## Running tests

```bash
uv sync --extra dev
uv run pytest
```

## Open questions

- Elicitation depends on the MCP client supporting it; if a client doesn't,
  `commit_and_push`/`create_pr` return a "declined or not supported" result
  rather than acting.
- PyGithub is used for GitHub access; an `httpx`-against-REST fallback could
  be added to reduce dependency weight.
- Fork creation hits the GitHub API each time a fresh fork is needed; there is
  a small propagation wait after creating a fork before it's usable.
