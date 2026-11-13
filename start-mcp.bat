@echo off
:: start-mcp.bat — Start the issue_agent MCP server (Windows CMD)
:: Delegates to the cross-platform Python launcher for reliable graceful shutdown.

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

:: Prefer the Python launcher (handles CTRL+C / graceful shutdown on all platforms).
if exist "start-mcp.py" (
    python start-mcp.py %*
    goto :end
)

:: Fallback: PowerShell launcher.
if exist "start-mcp.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File start-mcp.ps1 %*
    goto :end
)

echo ERROR: No launcher script found (start-mcp.py or start-mcp.ps1).
goto :end

:end
endlocal
