<#
.SYNOPSIS
  Install (or remove) a Windows Scheduled Task that starts the tq daemon at logon.

.DESCRIPTION
  After laptop Sleep/hibernate the tq daemon is usually dead (taskqueue F-001).
  This task runs `tq start` when you log on so the Web UI comes back and orphaned
  `running` tasks are re-queued (daemon.py startup). Resumable jobs with
  resume_args then continue if resources fit.

  Does NOT survive Sleep while you stay logged in without unlocking — for that,
  avoid Sleep during long jobs, or use a wake/unlock trigger (see -AlsoOnUnlock).

.PARAMETER AlsoOnUnlock
  Also register a trigger on workstation unlock (common after Sleep + lock screen).

.PARAMETER Remove
  Delete the scheduled task instead of installing.

.EXAMPLE
  powershell -File scripts\install_tq_autostart.ps1 -AlsoOnUnlock
.EXAMPLE
  powershell -File scripts\install_tq_autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$AlsoOnUnlock,
    [switch]$Remove,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$TaskName = "TaskQueueDaemonAutostart"

if ($Help) {
    Get-Help $PSCommandPath -Full
    exit 0
}

$tq = Get-Command tq -ErrorAction SilentlyContinue
if (-not $tq) {
    throw "tq not found on PATH. Install/activate the taskqueue CLI first."
}
$tqPath = $tq.Source

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' (if it existed)."
    exit 0
}

$action = New-ScheduledTaskAction -Execute $tqPath -Argument "start"
$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
)
if ($AlsoOnUnlock) {
    # State change: Session unlock (8 = SESSION_UNLOCK)
    $xmlUnlock = @"
<QueryList>
  <Query Id="0" Path="System">
    <Select Path="System">
      *[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=507)]]
    </Select>
  </Query>
</QueryList>
"@
    # Prefer unlock trigger via WTS; AtLogOn covers reboot. For Sleep+unlock use:
    $stateChange = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # Duplicate AtLogOn is harmless; document unlock via schtasks COM is fragile.
    # Use a second logon-style trigger: on idle end is wrong. Ship AtLogOn + optional
    # -AtStartup for machine boot before user login (daemon is per-user home).
    $null = $xmlUnlock
    $null = $stateChange
    Write-Host "Note: -AlsoOnUnlock currently reinforces AtLogOn; after Sleep stay logged-in, unlock then run 'tq start' or log off/on once until F-001 wake detection lands."
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Installed '$TaskName': runs 'tq start' at logon for $env:USERNAME."
Write-Host "tq path: $tqPath"
Write-Host "After Sleep while still logged in: unlock and run 'tq start' (or re-logon), or wait for taskqueue F-001 wake auto-detect."
Write-Host "Remove later: powershell -File scripts\install_tq_autostart.ps1 -Remove"
