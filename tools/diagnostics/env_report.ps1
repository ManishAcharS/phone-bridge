<#
.SYNOPSIS
    PhoneBridge Phase 0 environment audit for a Windows PC.

.DESCRIPTION
    Read-only. Nothing is installed, changed, or sent anywhere.

    It reports exactly the facts Phase 0 needs:
      * Windows build, architecture, PowerShell version
      * Python (and whether it is 64-bit), pip, venv availability
      * JDK / Android SDK / adb / gradle, for building the phone app
      * Audio render and capture endpoints, including any virtual cable
      * Cameras, and whether OBS Studio (virtual camera backend) is present
      * Bluetooth radios and connected Bluetooth audio devices
      * Firewall state for the ports PhoneBridge uses
      * Local IPv4 addresses and the default gateway

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\diagnostics\env_report.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\diagnostics\env_report.ps1 -OutFile report.txt
#>

[CmdletBinding()]
param(
    [string]$OutFile
)

$ErrorActionPreference = 'SilentlyContinue'
$lines = New-Object System.Collections.Generic.List[string]

function Write-Report {
    param([string]$Text = '')
    Write-Host $Text
    $lines.Add($Text) | Out-Null
}

function Write-Section {
    param([string]$Title)
    Write-Report ''
    Write-Report ('=' * 70)
    Write-Report "  $Title"
    Write-Report ('=' * 70)
}

function Write-Item {
    param(
        [string]$Status,   # OK | WARN | MISSING | INFO
        [string]$Name,
        [string]$Detail = ''
    )
    $tag = switch ($Status) {
        'OK'      { '[ok]     ' }
        'WARN'    { '[warn]   ' }
        'MISSING' { '[missing]' }
        default   { '[info]   ' }
    }
    Write-Report ("  {0} {1,-26} {2}" -f $tag, $Name, $Detail)
}

function Try-Command {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

Write-Report 'PhoneBridge - Windows environment audit'
Write-Report ("Generated: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Report 'Read-only: this script installs and modifies nothing.'

# --------------------------------------------------------------------------
Write-Section 'Operating system'
# --------------------------------------------------------------------------
$os = Get-CimInstance Win32_OperatingSystem
if ($os) {
    Write-Item OK 'Product'       $os.Caption
    Write-Item OK 'Version'       ("{0} (build {1})" -f $os.Version, $os.BuildNumber)
    Write-Item OK 'Architecture'  $os.OSArchitecture
    Write-Item OK 'Total RAM'     ("{0:N1} GB" -f ($os.TotalVisibleMemorySize / 1MB))
} else {
    Write-Item INFO 'Product' 'unknown (CIM unavailable)'
}
Write-Item INFO 'PowerShell' $PSVersionTable.PSVersion.ToString()
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Item INFO 'Elevated' $(if ($isAdmin) { 'yes' } else { 'no (fine - nothing here needs admin)' })

# --------------------------------------------------------------------------
Write-Section 'Python'
# --------------------------------------------------------------------------
$python = Try-Command 'python'
if (-not $python) { $python = Try-Command 'python3' }
if ($python) {
    $pyVersion = (& $python --version 2>&1) -join ' '
    Write-Item OK 'Interpreter' "$python  ($pyVersion)"
    $bits = & $python -c "import struct,sys;print(struct.calcsize('P')*8)" 2>&1
    Write-Item OK 'Pointer width' "$bits-bit"
    $siteOk = & $python -c "import sys;print('yes' if sys.prefix!=sys.base_prefix else 'no')" 2>&1
    Write-Item INFO 'In a venv' "$siteOk"
    $pkgs = & $python -c "import importlib.metadata as m;print(','.join(sorted(d.metadata['Name'] for d in m.distributions() if d.metadata['Name'])))" 2>&1
    if ($pkgs) { Write-Item INFO 'Installed pkgs' $pkgs }
} else {
    Write-Item MISSING 'Interpreter' 'python not on PATH - install Python 3.11+ from python.org (tick "Add to PATH")'
}

# --------------------------------------------------------------------------
Write-Section 'Android build toolchain'
# --------------------------------------------------------------------------
$java = Try-Command 'java'
if ($java) {
    $jv = (& java -version 2>&1) -join ' '
    Write-Item OK 'JDK' "$jv"
} else {
    Write-Item MISSING 'JDK' 'Android Studio bundles one, or: winget install Microsoft.OpenJDK.17'
}

$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) {
    $guess = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
    if (Test-Path $guess) { $sdk = $guess }
}
if ($sdk -and (Test-Path $sdk)) {
    Write-Item OK 'Android SDK' $sdk
    $platforms = Get-ChildItem (Join-Path $sdk 'platforms') -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Name
    if ($platforms) { Write-Item INFO '  platforms' ($platforms -join ', ') }
    $buildTools = Get-ChildItem (Join-Path $sdk 'build-tools') -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Name
    if ($buildTools) { Write-Item INFO '  build-tools' ($buildTools -join ', ') }
} else {
    Write-Item MISSING 'Android SDK' 'install Android Studio (easiest) - needed to build the phone app'
}

$adb = Try-Command 'adb'
if (-not $adb -and $sdk) {
    $candidate = Join-Path $sdk 'platform-tools\adb.exe'
    if (Test-Path $candidate) { $adb = $candidate }
}
if ($adb) {
    Write-Item OK 'adb' $adb
    $devices = (& $adb devices 2>&1) -join '; '
    Write-Item INFO '  adb devices' $devices
} else {
    Write-Item MISSING 'adb' 'Android platform-tools - needed for USB development only'
}

$gradle = Try-Command 'gradle'
if ($gradle) { Write-Item OK 'gradle' $gradle } else { Write-Item INFO 'gradle' 'not standalone; Android Studio / gradlew provides it' }

# --------------------------------------------------------------------------
Write-Section 'Audio endpoints'
# --------------------------------------------------------------------------
$soundDevices = Get-CimInstance Win32_SoundDevice
if ($soundDevices) {
    foreach ($d in $soundDevices) {
        Write-Item INFO 'device' ("{0}  [{1}]" -f $d.Name, $d.Status)
    }
} else {
    Write-Item INFO 'device' 'none reported by CIM'
}

# Enumerate real endpoints (render + capture) from the MMDevice registry keys.
$mmRoot = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio'
foreach ($flow in @(@{Key = 'Render'; Label = 'playback' }, @{ Key = 'Capture'; Label = 'recording' })) {
    $path = Join-Path $mmRoot $flow.Key
    $endpoints = Get-ChildItem $path -ErrorAction SilentlyContinue
    if (-not $endpoints) { continue }
    foreach ($ep in $endpoints) {
        $props = Get-ItemProperty (Join-Path $ep.PSPath 'Properties') -ErrorAction SilentlyContinue
        $name = $props.'{a45c254e-df1c-4efd-8020-67d146a850e0},2'
        $state = (Get-ItemProperty $ep.PSPath -ErrorAction SilentlyContinue).DeviceState
        if ($name) {
            $stateText = switch ($state) { 1 { 'active' } 2 { 'disabled' } 4 { 'not-present' } 8 { 'unplugged' } default { "state=$state" } }
            Write-Item INFO $flow.Label ("{0}  ({1})" -f $name, $stateText)
        }
    }
}

$cable = Get-CimInstance Win32_SoundDevice |
    Where-Object { $_.Name -match 'CABLE|VoiceMeeter|Virtual Audio|VB-Audio' }
if ($cable) {
    Write-Item OK 'virtual cable' (($cable | Select-Object -ExpandProperty Name) -join '; ')
    Write-Item INFO '' 'Needed only for "phone microphone as a PC microphone". PC -> phone needs no driver.'
} else {
    Write-Item WARN 'virtual cable' 'none found - install VB-Audio Cable (free) before Phase 4 microphone work'
}

# --------------------------------------------------------------------------
Write-Section 'Cameras and virtual camera backends'
# --------------------------------------------------------------------------
$cameras = Get-PnpDevice -Class Camera, Image -Status OK -ErrorAction SilentlyContinue
if ($cameras) {
    foreach ($c in $cameras) { Write-Item INFO 'camera' $c.FriendlyName }
} else {
    Write-Item INFO 'camera' 'none enumerated (or Get-PnpDevice unavailable)'
}
$obsPaths = @(
    (Join-Path $env:ProgramFiles 'obs-studio'),
    (Join-Path ${env:ProgramFiles(x86)} 'obs-studio')
)
$obs = $obsPaths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($obs) {
    Write-Item OK 'OBS Studio' "$obs  (pyvirtualcam 'obs' backend available)"
} else {
    Write-Item WARN 'OBS Studio' 'not installed - needed as the Phase 4 virtual camera backend (or Unity Capture)'
}

# --------------------------------------------------------------------------
Write-Section 'Bluetooth'
# --------------------------------------------------------------------------
$bt = Get-PnpDevice -Class Bluetooth -Status OK -ErrorAction SilentlyContinue
if ($bt) {
    foreach ($d in $bt) { Write-Item INFO 'device' $d.FriendlyName }
} else {
    Write-Item INFO 'device' 'none enumerated'
}
$btAudio = Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue |
    Where-Object { $_.FriendlyName -match 'Bluetooth|Hands-Free|Headset|Stereo' }
if ($btAudio) {
    foreach ($d in $btAudio) { Write-Item INFO 'bt audio' $d.FriendlyName }
}

# --------------------------------------------------------------------------
Write-Section 'Network'
# --------------------------------------------------------------------------
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -ne '127.0.0.1' } |
    ForEach-Object {
        Write-Item INFO 'ipv4' ("{0}/{1} on '{2}'" -f $_.IPAddress, $_.PrefixLength, $_.InterfaceAlias)
    }
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($route) { Write-Item INFO 'default gw' $route.NextHop }
Write-Item INFO 'ports used' 'TCP 8765 outbound to phone; UDP 47823 inbound (discovery only)'

$fw = Get-NetFirewallProfile -ErrorAction SilentlyContinue
if ($fw) {
    foreach ($p in $fw) {
        Write-Item INFO 'fw profile' ("{0}: enabled={1}" -f $p.Name, $p.Enabled)
    }
}
$fwRule = Get-NetFirewallRule -DisplayName '*PhoneBridge*' -ErrorAction SilentlyContinue
if ($fwRule) {
    foreach ($r in $fwRule) { Write-Item OK 'fw rule' $r.DisplayName }
} else {
    Write-Item INFO 'fw rule' 'no PhoneBridge rule yet - Windows will prompt the first time the CLI listens on UDP 47823'
}

# --------------------------------------------------------------------------
Write-Section 'Summary'
# --------------------------------------------------------------------------
$problems = @()
if (-not $python) { $problems += 'Python missing -> the PC side cannot run' }
if (-not $java) { $problems += 'JDK missing -> cannot build the Android app' }
if (-not $sdk) { $problems += 'Android SDK missing -> cannot build the Android app' }
if (-not $adb) { $problems += 'adb missing -> no USB development forwarding (Wi-Fi still works)' }
if (-not $cable) { $problems += 'No virtual audio cable -> "phone mic as PC mic" (Phase 4) is blocked until one is installed' }
if (-not $obs) { $problems += 'OBS not installed -> virtual webcam (Phase 4) needs OBS or Unity Capture' }

if ($problems.Count -eq 0) {
    Write-Report '  Everything Phase 1-2 needs is present.'
} else {
    Write-Report '  Blocking or upcoming gaps:'
    foreach ($p in $problems) { Write-Report "    - $p" }
}
Write-Report ''
Write-Report '  Send this whole output back and the plan will be adjusted to your hardware.'

if ($OutFile) {
    $lines -join [Environment]::NewLine | Set-Content -Path $OutFile -Encoding UTF8
    Write-Host ''
    Write-Host "Saved to $OutFile"
}
