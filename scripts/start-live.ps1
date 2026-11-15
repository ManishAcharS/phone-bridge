<#
.SYNOPSIS
    One-click daily PhoneBridge: USB autostart or Wi-Fi discovery, then camera + mic + speaker.

.DESCRIPTION
    This wrapper finds a usable Python, checks the Windows pieces that must be
    installed by hand, and runs:

        phonebridge start --camera --mic --speaker --daily

    The Python client owns the phone-side sequence so it is testable and survives
    reconnects: pick one USB serial, adb forward, launch MainActivity with
    --ez autostart true, wait for an authenticated ping, then start all three
    paths. With no USB phone it listens for the Wi-Fi/hotspot beacon and tries the
    last working address. -HostName is the manual fallback.

    Nothing is installed, no driver is touched, no Windows audio default is
    changed, and no administrator rights are needed. KeepAlive is off by default.

.PARAMETER HostName
    Manual phone IP/hostname fallback. Empty means auto discovery. 127.0.0.1 means USB.

.PARAMETER Transport
    auto (default), usb, wifi, or hotspot. auto prefers a usable USB phone, then Wi-Fi.

.PARAMETER AdbSerial
    One USB serial from `adb devices -l`. Required when more than one USB phone is attached.

.PARAMETER Device
    Pinned phone id/name for Wi-Fi discovery when more than one paired phone is present.

.PARAMETER LoopbackDevice
    Output device substring for PC audio capture. Empty captures the default output,
    so the room still hears it. Use a second virtual cable for headphone-only audio.

.PARAMETER Python
    Optional Python executable path. Leave empty for automatic selection; the
    Windows py launcher is invoked as executable py with separate argument -3.

.PARAMETER SkipChecks
    Skip the OBS/VB-CABLE/package preflight checks. The bridge still reports a device
    it cannot open; this does not force a missing driver to exist.

.NOTES
    Exit codes: 0 clean stop, 1 positively missing Windows piece, 2 Python/phone
    launch problem, 130 Ctrl+C. A check that cannot run is a warning with the child
    exit code/stderr and does not block the bridge. The phone must be unlocked once
    for a cold autostart on Android 14+/HyperOS; an already running bridge can
    continue while locked.
#>
[CmdletBinding()]
param(
    [string]$HostName = '',
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [ValidateSet('auto', 'usb', 'wifi', 'hotspot')][string]$Transport = 'auto',
    [string]$AdbSerial = '',
    [string]$Device = '',
    [ValidateSet('low', '720p', 'high')][string]$Profile = 'low',
    [ValidateSet('fixed', 'variable')][string]$FpsMode = 'fixed',
    [switch]$KeepAlive,
    [switch]$Stats,
    [ValidateRange(0, 86400)][double]$Duration = 0,
    [ValidateRange(0, 300)][double]$DiscoveryTimeout = 6,
    [ValidateRange(1, 600)][double]$StartupTimeout = 45,
    [switch]$SkipChecks,
    [string]$Python,
    [string]$LoopbackDevice = ''
)

$ErrorActionPreference = 'Stop'
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

trap {
    Write-Host ''
    Write-Host '  the launcher hit an unexpected error:' -ForegroundColor Red
    Write-Host "    $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '  that is a bug in start-live.ps1. Manual fallback:' -ForegroundColor DarkGray
    Write-Host '    python -m phonebridge start --camera --mic --speaker --daily --route auto' -ForegroundColor DarkGray
    exit 1
}

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Bad  { param([string]$Text) Write-Host "  [fail] $Text" -ForegroundColor Red }
function Write-Warn { param([string]$Text) Write-Host "  [warn] $Text" -ForegroundColor Yellow }
function Write-Hint { param([string]$Text) Write-Host "         $Text" -ForegroundColor DarkGray }

function Get-ScriptDirectory {
    if ($PSScriptRoot) { return $PSScriptRoot }
    if ($MyInvocation.MyCommand.Path) { return (Split-Path -Parent $MyInvocation.MyCommand.Path) }
    return (Get-Location).ProviderPath
}

$script:RepoRoot = Split-Path -Parent (Get-ScriptDirectory)
$script:PythonCommandHelpers = Join-Path (Get-ScriptDirectory) 'python-command.ps1'
if (-not (Test-Path -LiteralPath $script:PythonCommandHelpers)) {
    throw "Python command helpers not found: $script:PythonCommandHelpers"
}
. $script:PythonCommandHelpers
$script:PythonProbes = Join-Path (Get-ScriptDirectory) 'python-probes.ps1'
if (-not (Test-Path -LiteralPath $script:PythonProbes)) {
    throw "Python probe helpers not found: $script:PythonProbes"
}
. $script:PythonProbes

function Invoke-Phonebridge {
    param([string[]]$Arguments)
    $bridgeArguments = @('-m', 'phonebridge') + $Arguments
    Invoke-PythonCommand -Executable $script:PythonExe -Prefix $script:PythonArgs -Arguments $bridgeArguments
}

function Test-ObsInstalled {
    foreach ($path in @(
            'C:\Program Files\OBS Studio\bin\64bit\obs64.exe',
            'C:\Program Files (x86)\OBS Studio\bin\64bit\obs64.exe')) {
        if (Test-Path $path) { return $path }
    }
    foreach ($key in @('HKLM:\SOFTWARE\OBS Studio', 'HKCU:\SOFTWARE\OBS Studio')) {
        if (Test-Path $key) { return $key }
    }
    return $null
}

Write-Host ''
Write-Host 'PhoneBridge - daily use' -ForegroundColor White
Write-Host '--------------------------------------------------------------'
Write-Host ''
Write-Step 'locating Python'
$resolved = Resolve-Python -RepoRoot $script:RepoRoot -PythonOverride $Python
if (-not $resolved) {
    Write-Bad 'no usable Python interpreter was found (needs Python 3.10 or newer).'
    Write-Hint 'install from https://www.python.org/downloads/windows/ and tick Add python.exe to PATH'
    Write-Hint 'then run: powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1'
    Write-Hint 'or pass -Python C:\Path\To\python.exe'
    exit 2
}
$script:PythonExe = $resolved.Exe
$script:PythonArgs = @($resolved.Args)
$pythonDisplay = Format-PythonCommand -Executable $script:PythonExe -Arguments $script:PythonArgs
if (-not $resolved.HasPackage) {
    Write-Bad "Python $($resolved.Version) at $pythonDisplay cannot import phonebridge"
    Write-Hint "fix: $(Format-PythonCommand -Executable $script:PythonExe -Arguments ($script:PythonArgs + @('-m', 'pip', 'install', '-e', '.\windows')))"
    Write-Hint 'or run scripts\setup-windows.ps1'
    exit 2
}
Write-Ok "Python $($resolved.Version) ($pythonDisplay) - phonebridge importable"

$failures = @()
if (-not $SkipChecks) {
    Write-Step 'Python packages for the live paths'
    $missing = Test-PythonPackages
    if ($missing -like 'unknown*') {
        Write-Warn "Python package check could not run: $missing"
        Write-Hint 'continuing; phonebridge will report its own missing package/device error'
    } elseif ($missing -eq 'ok') {
        Write-Ok 'pyvirtualcam, sounddevice and PyAudioWPatch are importable'
    } else {
        Write-Bad "missing Python packages: $missing"
        Write-Hint "fix: $(Format-PythonCommand -Executable $script:PythonExe -Arguments ($script:PythonArgs + @('-m', 'pip', 'install', 'pyvirtualcam', 'sounddevice', 'PyAudioWPatch')))"
        $failures += 'python packages'
    }

    Write-Step 'OBS Virtual Camera (not DroidCam)'
    $obs = Test-ObsInstalled
    if ($obs) {
        Write-Ok "OBS found: $obs"
        Write-Hint 'if the camera device is missing, open OBS once and start its Virtual Camera, then rerun'
    } else {
        Write-Bad 'OBS Studio was not found, so OBS Virtual Camera is unavailable'
        Write-Hint 'fix: install OBS Studio from https://obsproject.com/download, then select OBS Virtual Camera in Zoom/Meet'
        $failures += 'OBS'
    }

    Write-Step 'VB-CABLE microphone pair'
    $cable = Test-CableInstalled
    if ($cable -like 'found:*') {
        Write-Ok $cable.Substring(6)
    } elseif ($cable -like 'unknown*') {
        Write-Warn "VB-CABLE check could not run: $cable"
        Write-Hint 'continuing; phonebridge mic will report its own VB-CABLE/device error'
    } else {
        Write-Bad "VB-CABLE pair is missing or incomplete ($cable)"
        Write-Hint 'fix: install VB-CABLE from https://vb-audio.com/Cable/ and REBOOT'
        Write-Hint 'the phone mic writes to CABLE Input; Zoom/Meet must select CABLE Output'
        $failures += 'VB-CABLE'
    }

    if ($LoopbackDevice) {
        Write-Step "speaker capture device: $LoopbackDevice"
        $choice = Test-LoopbackChoice -Name $LoopbackDevice
        if ($choice -like 'found:*') {
            Write-Ok $choice.Substring(6)
        } elseif ($choice -like 'unknown*') {
            Write-Warn "speaker capture check could not run: $choice"
            Write-Hint 'continuing; phonebridge speaker will report its own loopback/device error'
        } else {
            Write-Bad "no loopback-capable output matches $LoopbackDevice ($choice)"
            Write-Hint 'fix: phonebridge speaker --list-loopback-devices, then pass the exact name substring'
            $failures += 'loopback device'
        }
    } else {
        Write-Hint 'speaker captures the default PC output, so the room still hears it and Windows mute silences both.'
        Write-Hint 'for headphone-only audio install a second virtual cable and pass -LoopbackDevice <name>.'
    }
}

if ($failures.Count -gt 0) {
    Write-Host ''
    Write-Bad ('not starting: ' + ($failures -join ', '))
    Write-Hint 'fix the [fail] lines above, or use -SkipChecks to let the bridge report the device error itself'
    exit 1
}

$arguments = @(
    'start',
    '--camera', '--mic', '--speaker',
    '--daily',
    '--route', $Transport,
    '--port', "$Port",
    '--profile', $Profile,
    '--fps-mode', $FpsMode,
    '--discovery-timeout', "$DiscoveryTimeout",
    '--startup-timeout', "$StartupTimeout"
)
if ($HostName) { $arguments += @('--host', $HostName) }
if ($AdbSerial) { $arguments += @('--adb-serial', $AdbSerial) }
if ($Device) { $arguments += @('--device', $Device) }
if ($KeepAlive) { $arguments += '--keep-alive' }
if ($Stats) { $arguments += '--stats' }
if ($LoopbackDevice) { $arguments += @('--loopback-device', $LoopbackDevice) }
if ($Duration -gt 0) { $arguments += @('--duration', "$Duration") }

Write-Host ''
Write-Step 'phone discovery/autostart, authenticated ping, then camera + microphone + speaker'
Write-Hint 'a dropped link reconnects on its own; USB re-creates adb forward and Wi-Fi can rediscover'
Write-Hint 'cold autostart on Android 14+/HyperOS: unlock the phone once if the ping waits'
Write-Hint 'in the other app: camera = OBS Virtual Camera, microphone = CABLE Output'
Write-Host ''

try {
    Invoke-Phonebridge $arguments
    $code = $LASTEXITCODE
} catch {
    Write-Bad "$($_.Exception.Message)"
    $code = 1
}

Write-Host ''
if ($code -eq 0) {
    Write-Ok 'stopped cleanly'
} elseif ($code -eq 130 -or $code -eq -1073741510) {
    Write-Ok 'stopped (Ctrl+C)'
    $code = 0
} else {
    Write-Bad "the bridge stopped with exit code $code"
    Write-Hint 'read the [fail]/hint lines above; they name the missing phone, bridge, OBS or VB-CABLE piece'
}
exit $code
