# start-mcp.ps1 — Start the issue_agent MCP server (Windows PowerShell)
# Checks for .env, starts the server, and shuts down gracefully on CTRL+C / close.

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Push-Location $scriptDir

$envFile = Join-Path $scriptDir ".env"
$envExample = Join-Path $scriptDir ".env.example"

# ---------------------------------------------------------------------------
# 1. Ensure .env exists
# ---------------------------------------------------------------------------
if (!(Test-Path $envFile)) {
    Write-Host "ERROR: .env file not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please create it first based on $envExample:"
    Write-Host "  Copy-Item $envExample $envFile"
    Write-Host ""
    Write-Host "Then edit $envFile and fill in your values (at minimum GITHUB_TOKEN)."
    Write-Host "After that, run this script again."
    exit 1
}

# ---------------------------------------------------------------------------
# 2. Start the MCP server
# ---------------------------------------------------------------------------
Write-Host "Starting issue_agent MCP server..."
Write-Host "  (bind: http://127.0.0.1:8765/mcp)"
Write-Host "  Press CTRL+C to stop."
Write-Host ""

$serverProcess = $null

function Cleanup {
    param([int]$exitCode = 0)
    if ($null -ne $serverProcess -and -not $serverProcess.HasExited) {
        Write-Host ""
        Write-Host "Shutting down MCP server (PID $($serverProcess.Id)) gracefully..."
        try {
            # Ask the server to terminate gracefully.
            $serverProcess.CloseMainWindow() | Out-Null
            # Give it up to ~10 seconds to exit.
            $waited = 0
            while (-not $serverProcess.HasExited -and $waited -lt 20) {
                Start-Sleep -Milliseconds 500
                $waited++
            }
            # Force kill if it did not exit in time.
            if (-not $serverProcess.HasExited) {
                Write-Host "Server did not exit in time; forcing termination."
                $serverProcess.Kill()
            }
        } catch {
            # Process may already be gone.
        }
        Write-Host "MCP server terminated."
    }
    exit $exitCode
}

# Trap CTRL+C and CTRL+BREAK.
$host.UI.RawUI.KeyAvailable = $false
Register-EngineEvent PowerShell.Exiting -SourceIdentifier "McpExiting" -Action {
    Cleanup -exitCode 0
} | Out-Null

try {
    # Start uv run issue-agent as a child process (no new window).
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = "uv"
    $startInfo.Arguments = "run issue-agent"
    $startInfo.WorkingDirectory = $scriptDir
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $serverProcess = [System.Diagnostics.Process]::Start($startInfo)

    # Stream server output to the console so the user sees logs.
    $reader = $serverProcess.StandardOutput
    $errorReader = $serverProcess.StandardError

    # Read output asynchronously until the process exits.
    while (-not $serverProcess.HasExited) {
        if ($reader.Peek() -ge 0) {
            Write-Host ($reader.ReadLine())
        }
        if ($errorReader.Peek() -ge 0) {
            Write-Host ($errorReader.ReadLine())
        }
        Start-Sleep -Milliseconds 200
    }

    # Drain any remaining output.
    while ($reader.Peek() -ge 0) { Write-Host ($reader.ReadLine()) }
    while ($errorReader.Peek() -ge 0) { Write-Host ($errorReader.ReadLine()) }
} catch {
    Write-Host "Failed to start MCP server: $($_.Exception.Message)" -ForegroundColor Red
    Cleanup -exitCode 1
}

Cleanup -exitCode 0
