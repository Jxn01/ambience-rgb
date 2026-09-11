<#
.SYNOPSIS
    Install ambience-rgb on Windows: the venv, OpenRGB's settings, and three
    scheduled tasks.

.DESCRIPTION
    The Windows twin of scripts/install.sh, and idempotent like it: safe to
    re-run after pulling -- and it must be re-run after creating or moving a
    config, because the shutdown task is handed the config's path.

    1. Builds .venv with the py launcher, WITHOUT system site-packages. On
       Windows the hid binding comes from pip (the package "hidapi", which is
       cython-hidapi). That is the exact opposite of the Linux rule, where pip
       must never be allowed near it.
    2. Sets what ambience-rgb needs in OpenRGB's settings -- the Commander
       Core detectors off, and the red/green swap on for the zones the config
       lists in openrgb_rg_swap -- with the same Python code install.sh runs,
       creating the file when OpenRGB has never run. Its OpenRGB task is
       stopped for the edit: OpenRGB reads the file only when it starts.
    3. Reports other software that would fight for the same devices -- iCUE's
       Corsair Service, SignalRGB -- and with -DisableCompetitors stops and
       disables their services.
    4. Registers the tasks defined in tasks.ps1, and starts the first two:
         ambience-rgb             the daemon; at logon, as you, under pythonw
         ambience-rgb-openrgb     the OpenRGB SDK server; at logon, elevated
         ambience-rgb-lights-off  SYSTEM, on System event 1074 (shutdown, restart)
    5. Prints the line matterlights' .env needs.

    Run it from an elevated PowerShell: the SYSTEM task and the elevated
    OpenRGB task cannot be registered otherwise. Windows PowerShell 5.1 and
    PowerShell 7 both work.

.PARAMETER OpenRgb
    Path to OpenRGB.exe. Found automatically on PATH and in the usual install
    locations; beyond that the script does not guess.

.PARAMETER NoOpenRgbTask
    Do not register the OpenRGB server task, for running OpenRGB some other
    way. It must then serve the SDK on the config's host and port.

.PARAMETER DisableCompetitors
    Stop and disable the services of other RGB software that were found.
    Without it they are only reported, with the commands to do it by hand.
#>
param(
    [string]$OpenRgb,
    [switch]$NoOpenRgbTask,
    [switch]$DisableCompetitors
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "tasks.ps1")

# Python prints paths; both ends must agree that they are UTF-8.
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$venv = Join-Path $repoRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host $Text -ForegroundColor Cyan
}

function Write-Caution([string]$Text) {
    Write-Host $Text -ForegroundColor Yellow
}

function Get-VenvValue([string]$Code) {
    $output = & $python -c $Code
    if ($LASTEXITCODE -ne 0) { throw "The venv's python failed to run: $Code" }
    return ($output | Select-Object -Last 1)
}

function Restart-AmbienceTask([string]$Name) {
    # The tasks' policy is IgnoreNew: a start issued while the task is still
    # stopping is silently dropped. So stop it, wait for it to leave Running
    # (bounded), and only then start it.
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 50; $i++) {
        if ((Get-ScheduledTask -TaskName $Name).State -ne "Running") { break }
        Start-Sleep -Milliseconds 200
    }
    Start-ScheduledTask -TaskName $Name
}

function Stop-AmbienceOpenRgb {
    # OpenRGB reads OpenRGB.json once, as it detects devices, and writes the
    # whole file back from memory whenever it saves a setting: an edit made
    # while it runs does not apply until it restarts, and can be overwritten
    # before then. So our task is stopped for the edit -- step 6 starts it
    # again -- and an OpenRGB running any other way is reported, not touched.
    # Returns whether it stopped the task.
    $stopped = $false
    if (-not $NoOpenRgbTask) {
        $task = Get-ScheduledTask -TaskName $AmbienceOpenRgbTask -ErrorAction SilentlyContinue
        if ($null -ne $task -and "$($task.State)" -eq "Running") {
            Stop-ScheduledTask -TaskName $AmbienceOpenRgbTask -ErrorAction SilentlyContinue
            $stopped = $true
        }
    }
    # Stopping the task ends the process without waiting for it to be gone.
    for ($i = 0; $stopped -and $i -lt 50; $i++) {
        if (-not (Get-Process -Name OpenRGB -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 200
    }
    if ($stopped) {
        Write-Host "  stopped $AmbienceOpenRgbTask for the edit; step 6 starts it again"
    }
    $running = @(Get-Process -Name OpenRGB -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        $ids = ($running | ForEach-Object { $_.Id }) -join ", "
        Write-Caution "  OpenRGB is running outside the $AmbienceOpenRgbTask task (process $ids). It reads these settings only when it starts, and can write them back from memory: close it now, and start it again after this."
    }
    return $stopped
}

# -----------------------------------------------------------------------------
# 0. Elevation, and whose tasks these are
# -----------------------------------------------------------------------------
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell. The shutdown task runs as SYSTEM and the OpenRGB task runs elevated, and neither can be registered otherwise."
}
# The account the daemon runs as, taken from the token rather than typed. An
# account's name and its profile folder's name need not match, and only the
# account name is something Task Scheduler can map.
$userId = $identity.Name

# -----------------------------------------------------------------------------
# 1. Python and the venv
# -----------------------------------------------------------------------------
Write-Step "1. Python"
if (-not (Test-Path $python)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $launcher) {
        throw "The Python launcher (py.exe) is not installed. Install Python 3.11 or newer from python.org, then re-run."
    }
    & $launcher.Source -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) { throw "ambience-rgb needs Python 3.11 or newer, and 'py -3' is older." }
    # Plain -m venv, deliberately: see the description above.
    & $launcher.Source -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create $venv" }
    Write-Host "  created $venv"
}
& $python -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip could not upgrade itself" }
& $python -m pip install --quiet -e $repoRoot
if ($LASTEXITCODE -ne 0) { throw "pip install -e $repoRoot failed" }
# The "hid" module must be cython-hidapi. A package that is merely NAMED hid
# imports fine and has neither of these, which is the whole trap.
& $python -c "import hid; hid.device; hid.enumerate"
if ($LASTEXITCODE -ne 0) {
    throw "The 'hid' module in $venv is not cython-hidapi; another package named 'hid' may be shadowing it. Remove it with: & '$python' -m pip uninstall hid -- then re-run."
}
Write-Host ("  " + (Get-VenvValue "import sys; print(sys.version.split()[0])") + " in $venv")

# -----------------------------------------------------------------------------
# 2. OpenRGB's settings
# -----------------------------------------------------------------------------
Write-Step "2. OpenRGB settings"
# The same Python code install.sh runs: the Commander Core detectors off, and
# the red/green swap on for every zone the config lists in openrgb_rg_swap. It
# backs the file up before changing it, and creates it when OpenRGB has never
# run -- so OpenRGB's first run starts from these settings.
$openRgbStopped = Stop-AmbienceOpenRgb
$openRgbSettings = Join-Path $env:APPDATA "OpenRGB\OpenRGB.json"
& $python -m ambience_rgb.openrgb_settings $openRgbSettings
if ($LASTEXITCODE -ne 0) {
    Write-Caution "  $openRgbSettings was not fully updated; see above. Without it OpenRGB may take the Commander Core, and zones the config swaps back may show red as green."
}

# -----------------------------------------------------------------------------
# 3. Other software that drives the same devices
# -----------------------------------------------------------------------------
Write-Step "3. Other RGB software"
# Exactly one program may drive each device, and nothing enforces it: a second
# driver on the Commander Core opens it just as successfully, and the two then
# interleave -- colours that never apply, and no error anywhere.
$competitors = @(
    @{ Name = "CorsairService"; What = "iCUE's Corsair Service" },
    @{ Name = "CorsairDeviceControlService"; What = "iCUE's device control" },
    @{ Name = "SignalRgb.Service"; What = "SignalRGB's service" }
)
$contended = $false
foreach ($competitor in $competitors) {
    $service = Get-Service -Name $competitor.Name -ErrorAction SilentlyContinue
    if ($null -eq $service) { continue }
    $startType = "$($service.StartType)"
    $running = ("$($service.Status)" -eq "Running")
    if (-not $running -and $startType -ne "Automatic") {
        Write-Host ("  {0} ({1}): {2}, not running" -f $competitor.Name, $competitor.What, $startType)
        continue
    }
    if ($DisableCompetitors) {
        Stop-Service -Name $competitor.Name -Force -ErrorAction SilentlyContinue
        Set-Service -Name $competitor.Name -StartupType Disabled
        Write-Host ("  {0} ({1}): stopped and disabled." -f $competitor.Name, $competitor.What)
        Write-Host ("      To undo: Set-Service -Name '{0}' -StartupType {1}; Start-Service -Name '{0}'" -f $competitor.Name, $startType)
    } else {
        $contended = $true
        Write-Caution ("  {0} ({1}) is {2}, start type {3}." -f $competitor.Name, $competitor.What, $service.Status, $startType)
    }
}
foreach ($process in @("iCUE", "SignalRgb", "SignalRgbLauncher")) {
    if (Get-Process -Name $process -ErrorAction SilentlyContinue) {
        $contended = $true
        Write-Caution "  $process is running. Close it, and keep it from starting with Windows."
    }
}
if ($contended) {
    Write-Caution "  Re-run with -DisableCompetitors to stop and disable the services above, or do it by hand."
}

# -----------------------------------------------------------------------------
# 4. OpenRGB itself
# -----------------------------------------------------------------------------
Write-Step "4. OpenRGB"
if (-not $NoOpenRgbTask) {
    if (-not $OpenRgb) {
        $candidates = @()
        $onPath = Get-Command OpenRGB.exe -ErrorAction SilentlyContinue
        if ($null -ne $onPath) { $candidates += $onPath.Source }
        foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA "Programs"))) {
            if ($root) { $candidates += (Join-Path $root "OpenRGB\OpenRGB.exe") }
        }
        $winget = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
        if (Test-Path $winget) {
            $candidates += @(Get-ChildItem -Path $winget -Filter "OpenRGB.exe" -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        }
        $OpenRgb = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
    if (-not $OpenRgb -or -not (Test-Path $OpenRgb)) {
        Write-Caution "  OpenRGB.exe was not found, so its server task is not registered. The Commander Core and the soundbar do not need it; the RAM, GPU, board, mouse and monitor do. Install OpenRGB, then re-run this, or pass -OpenRgb 'path\to\OpenRGB.exe'."
        $NoOpenRgbTask = $true
    } else {
        Write-Host "  $OpenRgb"
    }
}

# -----------------------------------------------------------------------------
# 5. Config, endpoint, logs
# -----------------------------------------------------------------------------
Write-Step "5. Config"
# The shutdown task runs as SYSTEM, which has neither your profile nor your
# environment to search, so it is handed the file the daemon reads. Re-run
# this after creating or moving a config.
$config = Get-VenvValue "from ambience_rgb.config import resolved_config_path; print(resolved_config_path())"
$endpoint = Get-VenvValue "from ambience_rgb.config import load_config; print(load_config().endpoint)"
$openRgbPort = [int](Get-VenvValue "from ambience_rgb.config import load_config; print(load_config().openrgb_port)")
$logDirectory = Join-Path $env:LOCALAPPDATA "ambience-rgb"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$daemonLog = Join-Path $logDirectory "ambience-rgb.log"
$shutdownLog = Join-Path $logDirectory "lights-off.log"
Write-Host "  config    $config"
Write-Host "  endpoint  $endpoint"
Write-Host "  logs      $logDirectory"

# -----------------------------------------------------------------------------
# 6. Scheduled tasks
# -----------------------------------------------------------------------------
Write-Step "6. Scheduled tasks"
# A daemon from an earlier install is stopped first, gracefully: it blacks
# everything and lets go of the soundbar's COM port, which Windows opens
# exclusively. Logged to the shutdown log, not this window.
& $python -m ambience_rgb --config $config --log-file $shutdownLog off --stop-daemon

if ($NoOpenRgbTask) {
    if (Get-ScheduledTask -TaskName $AmbienceOpenRgbTask -ErrorAction SilentlyContinue) {
        Write-Host "  $AmbienceOpenRgbTask is left as it was"
    }
} else {
    $xml = New-AmbienceOpenRgbTaskXml -UserId $userId -OpenRgb $OpenRgb -WorkingDirectory (Split-Path -Parent $OpenRgb) -Port $openRgbPort
    Register-ScheduledTask -TaskName $AmbienceOpenRgbTask -Xml $xml -Force -ErrorAction Stop | Out-Null
    Write-Host "  registered $AmbienceOpenRgbTask"
}
$xml = New-AmbienceDaemonTaskXml -UserId $userId -Pythonw $pythonw -RepoRoot $repoRoot -LogFile $daemonLog
Register-ScheduledTask -TaskName $AmbienceDaemonTask -Xml $xml -Force -ErrorAction Stop | Out-Null
Write-Host "  registered $AmbienceDaemonTask"
$xml = New-AmbienceShutdownTaskXml -Pythonw $pythonw -RepoRoot $repoRoot -Config $config -LogFile $shutdownLog
Register-ScheduledTask -TaskName $AmbienceShutdownTask -Xml $xml -Force -ErrorAction Stop | Out-Null
Write-Host "  registered $AmbienceShutdownTask"

if (-not $NoOpenRgbTask) {
    Restart-AmbienceTask $AmbienceOpenRgbTask
} elseif ($openRgbStopped) {
    # Stopped for the settings edit in step 2, and then OpenRGB.exe was not
    # found in step 4: start the task that was running rather than leave the
    # case half dark.
    Start-ScheduledTask -TaskName $AmbienceOpenRgbTask
}
Restart-AmbienceTask $AmbienceDaemonTask
Write-Host "  started"

Write-Step "Done."
Write-Host @"
  matterlights must be told to publish. In its .env:

      RGB_EXTENSION_ENABLED=true
      RGB_PUBLISH_SOCKET=$endpoint

  then restart matterlights' sync: its dashboard can, or by hand stop the
  "MatterLights Screen Sync" task, wait until it shows Ready, and start it.

  What is driven:  & '$python' -m ambience_rgb devices
                   (stop the daemon first to look at the AIO and the soundbar)
  Stop, black:     & '$python' -m ambience_rgb off --stop-daemon
  Start again:     Start-ScheduledTask -TaskName $AmbienceDaemonTask
  Logs:            $daemonLog
                   $shutdownLog
  The shutdown task without a reboot (it stops the daemon and blacks
  everything):     Start-ScheduledTask -TaskName $AmbienceShutdownTask
"@
