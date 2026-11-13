#!/usr/bin/env bash
# start-mcp.sh — Start the issue_agent MCP server (Mac / Linux)
# Checks for .env, starts the server, and shuts down gracefully on CTRL+C / close.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

# ---------------------------------------------------------------------------
# 1. Ensure .env exists
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env file not found."
    echo ""
    echo "Please create it first based on $ENV_EXAMPLE:"
    echo "  cp $ENV_EXAMPLE $ENV_FILE"
    echo ""
    echo "Then edit $ENV_FILE and fill in your values (at minimum GITHUB_TOKEN)."
    echo "After that, run this script again."
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Start the MCP server
# ---------------------------------------------------------------------------
echo "Starting issue_agent MCP server..."
echo "  (bind: http://127.0.0.1:8765/mcp)"
echo "  Press CTRL+C to stop."
echo ""

SERVER_PID=""

cleanup() {
    local code="${1:-0}"
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo ""
        echo "Shutting down MCP server (PID $SERVER_PID) gracefully..."
        # Ask the server to terminate gracefully (SIGTERM).
        kill -TERM "$SERVER_PID" 2>/dev/null || true

        # Wait up to ~10 seconds for it to exit on its own.
        local waited=0
        while kill -0 "$SERVER_PID" 2>/dev/null && [[ $waited -lt 20 ]]; do
            sleep 0.5
            waited=$((waited + 1))
        done

        # Force kill if it did not exit in time.
        if kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "Server did not exit in time; forcing termination."
            kill -KILL "$SERVER_PID" 2>/dev/null || true
            wait "$SERVER_PID" 2>/dev/null || true
        fi
        echo "MCP server terminated."
    fi
    exit "$code"
}

# Trap interrupt (CTRL+C) and termination signals.
trap 'cleanup 0' INT TERM
# Also trap ERR so a crash still attempts cleanup of the server.
trap 'cleanup 1' ERR

uv run issue-agent &
SERVER_PID=$!

# Wait for the server process to exit (or be killed by cleanup).
wait "$SERVER_PID" 2>/dev/null || true
cleanup 0
