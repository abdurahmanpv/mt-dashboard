<#
.SYNOPSIS
    One-time setup: registers the CEO Dashboard refresh as a Windows Scheduled Task.

.DESCRIPTION
    Creates a task named "CEO Dashboard Daily Refresh" that runs run_refresh.ps1
    every day at 3:00 AM Pacific Time.

    The script auto-converts 3 AM PST/PDT to your machine's local timezone, so
    it works correctly regardless of where the server is configured.

    The task runs as the current user, whether logged in or not (S4U logon).
    If prompted for a password, enter your Windows account password so Task
    Scheduler can run the job unattended.

.REQUIREMENTS
    Run PowerShell as Administrator.

.USAGE
    Right-click PowerShell -> "Run as Administrator"
    cd "C:\Users\ABDURRAHMAN PV\OneDrive - way\Tasks\Data Team - Documents\Mileage Tracker\CEO Dashboard\files"
    .\setup_task.ps1

    To remove the task later:
    Unregister-ScheduledTask -TaskName "CEO Dashboard Daily Refresh" -Confirm:$false
#>

$ErrorActionPreference = "Stop"

# ── Task identity ─────────────────────────────────────────────────────────────
$TaskName   = "CEO Dashboard Daily Refresh"

# ── Paths ─────────────────────────────────────────────────────────────────────
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RunScript  = Join-Path $ScriptDir "run_refresh.ps1"

if (-not (Test-Path $RunScript)) {
    Write-Error "run_refresh.ps1 not found at: $RunScript"
    exit 1
}

# ── Convert 3:00 AM Pacific to the machine's local time ──────────────────────
# Uses "Pacific Standard Time" zone ID — Windows auto-applies DST (PST/PDT).
$pacificZone = [TimeZoneInfo]::FindSystemTimeZoneById("Pacific Standard Time")
$today       = [DateTime]::Today
$pst3am      = [DateTime]::new($today.Year, $today.Month, $today.Day, 3, 0, 0)
$utc3am      = [TimeZoneInfo]::ConvertTimeToUtc($pst3am, $pacificZone)
$local3am    = [TimeZoneInfo]::ConvertTimeFromUtc($utc3am, [TimeZoneInfo]::Local)

Write-Host ""
Write-Host "Machine timezone : $([TimeZoneInfo]::Local.DisplayName)"
Write-Host "Trigger time     : $($local3am.ToString('HH:mm')) local  (= 03:00 Pacific)"
Write-Host ""

# ── Build task components ─────────────────────────────────────────────────────
$trigger = New-ScheduledTaskTrigger -Daily -At $local3am

$action  = New-ScheduledTaskAction `
               -Execute   "powershell.exe" `
               -Argument  "-NonInteractive -NoProfile -ExecutionPolicy Bypass -File `"$RunScript`"" `
               -WorkingDirectory $ScriptDir

$settings = New-ScheduledTaskSettingsSet `
                -ExecutionTimeLimit  (New-TimeSpan -Hours 1) `
                -RestartCount        2 `
                -RestartInterval     (New-TimeSpan -Minutes 5) `
                -StartWhenAvailable                           `
                -WakeToRun           $false                   `
                -MultipleInstances   IgnoreNew

# S4U: runs as the current user without storing credentials in plaintext.
# The task runs whether the user is logged in or not.
$principal = New-ScheduledTaskPrincipal `
                 -UserId  ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
                 -LogonType S4U `
                 -RunLevel Highest

# ── Register (replace if already exists) ─────────────────────────────────────
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Task already exists — replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Trigger     $trigger `
    -Action      $action `
    -Settings    $settings `
    -Principal   $principal `
    -Description "Refreshes the CEO Subscription Dashboard from MySQL daily at 3 AM Pacific."

$nextRun = (Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo).NextRunTime
Write-Host ""
Write-Host "Task '$TaskName' registered successfully."
Write-Host "Next scheduled run: $nextRun"
Write-Host ""
Write-Host "To run immediately for testing:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To view logs after a run:"
Write-Host "  Get-Content '$ScriptDir\logs\refresh_$(Get-Date -Format yyyy-MM-dd).log'"
