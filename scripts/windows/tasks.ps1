<#
.SYNOPSIS
    The three scheduled tasks of ambience-rgb, as Task Scheduler XML.

.DESCRIPTION
    Dot-sourced by install.ps1. Every function here is pure: it takes paths
    and an account and returns the XML that Register-ScheduledTask -Xml is
    given. Nothing touches the system, so tests/test_windows_scripts.py renders
    each task with a portable pwsh on Linux and checks it -- and the XML that
    is registered is the XML that was tested.

    The task names are the systemd unit names, and ambience_rgb.hints carries
    the same three; a test keeps them equal.

    Written for Windows PowerShell 5.1 as well as PowerShell 7, and in plain
    ASCII: 5.1 reads a script without a byte-order mark as the ANSI code page.
#>

$AmbienceDaemonTask = "ambience-rgb"
$AmbienceOpenRgbTask = "ambience-rgb-openrgb"
$AmbienceShutdownTask = "ambience-rgb-lights-off"

function ConvertTo-AmbienceXmlText {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    [System.Security.SecurityElement]::Escape($Value)
}

function Get-AmbienceTaskSettingsXml {
    <#
    .SYNOPSIS
        The Settings block the three tasks share.
    .PARAMETER ExecutionTimeLimit
        PT0S -- no limit -- for the two that run all session. Task Scheduler's
        default is PT72H: it stops a task after three days, so a daemon left at
        the default would go dark on the fourth morning of an uptime.
    .PARAMETER RestartOnFailure
        Up to three restarts, a minute apart. This covers a failure to START
        only: Task Scheduler does not treat a non-zero exit as a failure.
    #>
    param(
        [Parameter(Mandatory)][string]$ExecutionTimeLimit,
        [switch]$RestartOnFailure
    )
    $restart = ""
    if ($RestartOnFailure) {
        $restart = @"
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>

"@
    }
    @"
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>$ExecutionTimeLimit</ExecutionTimeLimit>
    <Priority>5</Priority>
$restart  </Settings>
"@
}

function New-AmbienceDaemonTaskXml {
    <#
    .SYNOPSIS
        The daemon: at logon, as the user, under pythonw so no console window
        appears. It logs to -LogFile, since pythonw has no stderr at all.
    #>
    param(
        [Parameter(Mandatory)][string]$UserId,
        [Parameter(Mandatory)][string]$Pythonw,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$LogFile
    )
    $user = ConvertTo-AmbienceXmlText $UserId
    $command = ConvertTo-AmbienceXmlText $Pythonw
    $arguments = ConvertTo-AmbienceXmlText ('-m ambience_rgb --log-file "{0}" run' -f $LogFile)
    $directory = ConvertTo-AmbienceXmlText $RepoRoot
    $settings = Get-AmbienceTaskSettingsXml -ExecutionTimeLimit "PT0S" -RestartOnFailure
    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>ambience-rgb: drives the case lighting from the ambience frames matterlights publishes. Starts at logon, and blacks everything whenever it stops.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
$settings
  <Actions Context="Author">
    <Exec>
      <Command>$command</Command>
      <Arguments>$arguments</Arguments>
      <WorkingDirectory>$directory</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

function New-AmbienceOpenRgbTaskXml {
    <#
    .SYNOPSIS
        The OpenRGB SDK server: at logon, elevated, bound to the loopback.
    .DESCRIPTION
        Elevated because OpenRGB needs administrator rights on Windows to reach
        SMBus -- the RAM and the GPU. Bound to 127.0.0.1 because OpenRGB's own
        default listens on every interface, which puts control of this
        machine's lighting on the LAN.
    #>
    param(
        [Parameter(Mandatory)][string]$UserId,
        [Parameter(Mandatory)][string]$OpenRgb,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [int]$Port = 6742
    )
    $user = ConvertTo-AmbienceXmlText $UserId
    $command = ConvertTo-AmbienceXmlText $OpenRgb
    $arguments = ConvertTo-AmbienceXmlText ("--server --server-host 127.0.0.1 --server-port {0}" -f $Port)
    $directory = ConvertTo-AmbienceXmlText $WorkingDirectory
    $settings = Get-AmbienceTaskSettingsXml -ExecutionTimeLimit "PT0S" -RestartOnFailure
    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>ambience-rgb: the OpenRGB SDK server, on the loopback only. Elevated, because SMBus (the RAM, the GPU) needs it on Windows.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
$settings
  <Actions Context="Author">
    <Exec>
      <Command>$command</Command>
      <Arguments>$arguments</Arguments>
      <WorkingDirectory>$directory</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

function New-AmbienceShutdownTaskXml {
    <#
    .SYNOPSIS
        Lights off at shutdown and restart: SYSTEM, on System event 1074.
    .DESCRIPTION
        SYSTEM, in session 0, deliberately. By the time event 1074 is logged
        the user's session is already being torn down and Windows will not
        start a process in it -- matterlights registered its own first as the
        user and watched it fail with STATUS_DLL_INIT_FAILED_LOGOFF. It is also
        why the daemon's in-process session-end hook never fires at shutdown.

        It runs "off --stop-daemon": ask the daemon to stop, wait until it has
        blacked and released everything -- the soundbar's COM port opens
        exclusively on Windows -- then black everything once more itself.

        The principal is the SID S-1-5-18 rather than the account's name,
        which is localised.
    #>
    param(
        [Parameter(Mandatory)][string]$Pythonw,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$Config,
        [Parameter(Mandatory)][string]$LogFile
    )
    # System event 1074 from User32, raised for shutdown and restart alike.
    $subscription = @'
<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name='User32'] and (EventID=1074)]]</Select></Query></QueryList>
'@
    $query = ConvertTo-AmbienceXmlText $subscription
    $command = ConvertTo-AmbienceXmlText $Pythonw
    $arguments = ConvertTo-AmbienceXmlText ('-m ambience_rgb --config "{0}" --log-file "{1}" off --stop-daemon' -f $Config, $LogFile)
    $directory = ConvertTo-AmbienceXmlText $RepoRoot
    $settings = Get-AmbienceTaskSettingsXml -ExecutionTimeLimit "PT1M"
    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>ambience-rgb: turns the case lighting off when Windows begins shutting down or restarting.</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>$query</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
$settings
  <Actions Context="Author">
    <Exec>
      <Command>$command</Command>
      <Arguments>$arguments</Arguments>
      <WorkingDirectory>$directory</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}
