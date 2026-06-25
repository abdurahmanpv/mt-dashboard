<#
.SYNOPSIS
    CEO Subscription Dashboard — Daily Refresh Runner

.DESCRIPTION
    Activates the project venv, runs daily_refresh.py, and writes output to
    logs\refresh_YYYY-MM-DD.log.  Called by Windows Task Scheduler (see setup_task.ps1).
    Can also be run manually from any PowerShell window.

.USAGE
    .\run_refresh.ps1              # full run: MySQL -> Excel -> Dashboard
    .\run_refresh.ps1 --skip-db   # rebuild from existing Excel (no DB call)
    .\run_refresh.ps1 --dry-run   # validate config only, no changes made
#>

param(
    [switch]$SkipDb,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# ── Paths ─────────────────────────────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Python    = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$Script    = Join-Path $ScriptDir "daily_refresh.py"
$LogDir    = Join-Path $ScriptDir "logs"
$LogFile   = Join-Path $LogDir ("refresh_" + (Get-Date -Format "yyyy-MM-dd") + ".log")

# ── Ensure logs\ exists ───────────────────────────────────────────────────────
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

# ── Guard: venv python must exist ────────────────────────────────────────────
if (-not (Test-Path $Python)) {
    $msg = "ERROR: venv Python not found at $Python. Run: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt"
    $msg | Tee-Object -FilePath $LogFile -Append
    exit 1
}

# ── Build args list ───────────────────────────────────────────────────────────
$pyArgs = @($Script)
if ($SkipDb)  { $pyArgs += "--skip-db"  }
if ($DryRun)  { $pyArgs += "--dry-run"  }

# ── Run ───────────────────────────────────────────────────────────────────────
Set-Location $ScriptDir

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"" | Out-File -FilePath $LogFile -Append          # blank line separator
"[$stamp] ========================================" | Tee-Object -FilePath $LogFile -Append
"[$stamp]  CEO Dashboard refresh starting" | Tee-Object -FilePath $LogFile -Append
"[$stamp] ========================================" | Tee-Object -FilePath $LogFile -Append

& $Python @pyArgs 2>&1 | Tee-Object -FilePath $LogFile -Append

$exitCode = $LASTEXITCODE

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
if ($exitCode -eq 0) {
    "[$stamp] Finished successfully (exit 0)" | Tee-Object -FilePath $LogFile -Append
} else {
    "[$stamp] FAILED with exit code $exitCode" | Tee-Object -FilePath $LogFile -Append
}

# ── Push updated HTML to GitHub (only on success, only if file changed) ───────
if ($exitCode -eq 0 -and -not $DryRun) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$stamp] Checking for HTML changes to push to GitHub..." | Tee-Object -FilePath $LogFile -Append

    $htmlFile = Join-Path $ScriptDir "CEO_Subscription_Dashboard.html"
    if (Test-Path $htmlFile) {
        git -C $ScriptDir add CEO_Subscription_Dashboard.html 2>&1 | Tee-Object -FilePath $LogFile -Append

        # Only commit if the file actually changed
        $diffOutput = git -C $ScriptDir diff --cached --quiet 2>&1
        if ($LASTEXITCODE -ne 0) {
            $dateStr = Get-Date -Format "yyyy-MM-dd"
            git -C $ScriptDir commit -m "chore: refresh dashboard $dateStr" 2>&1 | Tee-Object -FilePath $LogFile -Append
            git -C $ScriptDir push origin main 2>&1 | Tee-Object -FilePath $LogFile -Append

            if ($LASTEXITCODE -eq 0) {
                $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
                "[$stamp] HTML pushed to GitHub — Pages will update shortly." | Tee-Object -FilePath $LogFile -Append
            } else {
                $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
                "[$stamp] WARNING: git push failed (exit $LASTEXITCODE). HTML not published." | Tee-Object -FilePath $LogFile -Append
            }
        } else {
            $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            "[$stamp] HTML unchanged — skipping commit." | Tee-Object -FilePath $LogFile -Append
        }
    } else {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        "[$stamp] WARNING: CEO_Subscription_Dashboard.html not found — skipping push." | Tee-Object -FilePath $LogFile -Append
    }
}

# ── Prune logs older than 30 days ─────────────────────────────────────────────
Get-ChildItem -Path $LogDir -Filter "refresh_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force

exit $exitCode
