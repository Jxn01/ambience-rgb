<#
.SYNOPSIS
    Remove ambience-rgb's scheduled tasks and turn the hardware off.

.DESCRIPTION
    The Windows twin of scripts/uninstall.sh. It leaves the repository, the
    venv and OpenRGB's settings alone: the Commander Core detectors stay off,
    so turn them back on in OpenRGB to let it see the AIO again. A service
    that install.ps1 -DisableCompetitors disabled stays disabled; the install
    printed how to undo each one.

    Run it from an elevated PowerShell: removing the SYSTEM task needs it.
#>
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "tasks.ps1")

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell: removing the SYSTEM shutdown task needs it."
}

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $python) {
    # Stop the daemon gracefully -- it blacks everything and lets go -- then
    # black once more. First, while OpenRGB's server is still up to reach.
    & $python -m ambience_rgb off --stop-daemon
}

foreach ($name in @($AmbienceDaemonTask, $AmbienceShutdownTask, $AmbienceOpenRgbTask)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed $name"
    } else {
        Write-Host "$name is not registered"
    }
}

Write-Host "Removed. OpenRGB's Commander Core detectors are still off; turn them back on in OpenRGB if you want it to see the AIO again."
