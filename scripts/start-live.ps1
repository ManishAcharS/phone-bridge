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

.PARAMETER SkipChecks
    Skip the advisory OBS/VB-CABLE/package checks. The bridge still reports a device
    it cannot open; this does not force a missing driver to exist.

.NOTES
    Exit codes: 0 clean stop, 1 missing Windows piece, 2 Python/package/phone launch
    problem, 130 Ctrl+C. The phone must be unlocked once for a cold autostart on
    Android 14+/HyperOS; an already running bridge can continue while locked.
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
function Write-Hint { param([string]$Text) Write-Host "         $Text" -ForegroundColor DarkGray }

function Get-ScriptDirectory {
    if ($PSScriptRoot) { return $PSScriptRoot }
    if ($MyInvocation.MyCommand.Path) { return (Split-Path -Parent $MyInvocation.MyCommand.Path) }
    return (Get-Location).ProviderPath
}

$script:RepoRoot = Split-Path -Parent (Get-ScriptDirectory)

function Invoke-PythonCandidate {
    param([string[]]$Command, [string[]]$Arguments)
    $exe = $Command[0]
    $prefix = @()
    if ($Command.Count -gt 1) { $prefix = @($Command | Select-Object -Skip 1) }
    return (& $exe @prefix @Arguments 2>$null)
}

function Test-Interpreter {
    param([string[]]$Command)
    try {
        $output = Invoke-PythonCandidate -Command $Command -Arguments @(
            '-c', 'import sys; print(sys.version_info[0], sys.version_info[1])'
        )
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
    $parts = ("$output").Trim().Split(' ')
    if ($parts.Count -lt 2) { return $null }
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) { return $null }
    return "$major.$minor"
}

function Test-PhonebridgeImportable {
    param([string[]]$Command)
    try {
        Invoke-PythonCandidate -Command $Command -Arguments @('-c', 'import phonebridge') | Out-Null
    } catch { return $false }
    return ($LASTEXITCODE -eq 0)
}

function Get-PythonCandidates {
    $candidates = @()
    if ($Python) { $candidates += , @($Python) }
    foreach ($venv in @('.venv', 'venv')) {
        $exe = Join-Path (Join-Path $script:RepoRoot $venv) 'Scripts\python.exe'
        if (Test-Path $exe) { $candidates += , @($exe) }
    }
    foreach ($name in @(@('py', '-3'), @('py'), @('python'), @('python3'))) {
        if (Get-Command $name[0] -ErrorAction SilentlyContinue) { $candidates += , $name }
    }
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Programs\Python') }
    if ($env:ProgramFiles) { $roots += $env:ProgramFiles }
    foreach ($root in $roots) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        foreach ($pattern in @('Python3*\python.exe', 'Python3*\python3.exe')) {
            $path = Join-Path $root $pattern
            foreach ($exe in @(Get-ChildItem -Path $path -ErrorAction SilentlyContinue)) {
                if ($exe.PSIsContainer) { continue }
                $candidates += , @($exe.FullName)
            }
        }
    }
    return $candidates
}

function Resolve-Python {
    $fallback = $null
    foreach ($candidate in (Get-PythonCandidates)) {
        $version = Test-Interpreter -Command $candidate
        if (-not $version) { continue }
        if (Test-PhonebridgeImportable -Command $candidate) {
            return @{ Command = $candidate; Version = $version; HasPackage = $true }
        }
        if (-not $fallback) {
            $fallback = @{ Command = $candidate; Version = $version; HasPackage = $false }
        }
    }
    return $fallback
}

function Invoke-Phonebridge {
    param([string[]]$Arguments)
    & $script:PythonCommand @('-m', 'phonebridge') @Arguments
}

function Test-PythonPackages {
    $probe = @'
import importlib
missing = []
for name in ("pyvirtualcam", "sounddevice", "pyaudiowpatch"):
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
print(",".join(missing))
'@
    try { $output = & $script:PythonCommand @('-c', $probe) 2>$null } catch { return 'unknown' }
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    return (($output | Select-Object -Last 1) -as [string])
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

function Test-CableInstalled {
    $probe = @'
try:
    import sounddevice
except Exception:
    print("unknown")
    raise SystemExit(0)
devices = list(sounddevice.query_devices())
out = [str(d["name"]) for d in devices if int(d.get("max_output_channels", 0)) > 0 and "cable input" in str(d["name"]).lower()]
inp = [str(d["name"]) for d in devices if int(d.get("max_input_channels", 0)) > 0 and "cable output" in str(d["name"]).lower()]
if out and inp:
    print("found:" + out[0] + " / " + inp[0])
elif out or inp:
    print("partial")
else:
    print("missing")
'@
    try { $output = & $script:PythonCommand @('-c', $probe) 2>$null } catch { return 'unknown' }
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    return (($output | Select-Object -Last 1) -as [string])
}

function Test-LoopbackChoice {
    param([string]$Name)
    $probe = @'
import sys
from phonebridge.live.devices import list_loopback_devices
name = sys.argv[1].lower()
devices = list_loopback_devices()
hit = [d["name"] for d in devices if name in str(d["name"]).lower()]
print("found:" + str(hit[0]) if hit else "missing")
'@
    try { $output = & $script:PythonCommand @('-c', $probe, $Name) 2>$null } catch { return 'unknown' }
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    return (($output | Select-Object -Last 1) -as [string])
}

Write-Host ''
Write-Host 'PhoneBridge - daily use' -ForegroundColor White
Write-Host '--------------------------------------------------------------'
Write-Host ''
Write-Step 'locating Python'
$resolved = Resolve-Python
if (-not $resolved) {
    Write-Bad 'no usable Python was found (needs Python 3.10 or newer).'
    Write-Hint 'install from https://www.python.org/downloads/windows/ and tick Add python.exe to PATH'
    Write-Hint 'then run: powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1'
    Write-Hint 'or pass -Python C:\Path\To\python.exe'
    exit 2
}
$script:PythonCommand = $resolved.Command
if (-not $resolved.HasPackage) {
    Write-Bad "Python $($resolved.Version) at $($script:PythonCommand -join ' ') cannot import phonebridge"
    Write-Hint "fix: $($script:PythonCommand -join ' ') -m pip install -e .\windows"
    Write-Hint 'or run scripts\setup-windows.ps1'
    exit 2
}
Write-Ok "Python $($resolved.Version) ($($script:PythonCommand -join ' ')) - phonebridge importable"

$failures = @()
if (-not $SkipChecks) {
    Write-Step 'Python packages for the live paths'
    $missing = Test-PythonPackages
    if ($missing -eq 'unknown' -or $missing) {
        Write-Bad "missing or uncheckable Python packages: $missing"
        Write-Hint "fix: $($script:PythonCommand -join ' ') -m pip install pyvirtualcam sounddevice PyAudioWPatch"
        $failures += 'python packages'
    } else {
        Write-Ok 'pyvirtualcam, sounddevice and PyAudioWPatch are importable'
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
