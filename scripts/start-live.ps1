<#
.SYNOPSIS
    One command for daily use: adb forward, check the PC, run the whole bridge.

.DESCRIPTION
    `phonebridge start --camera --mic --speaker` is the command; this launcher is
    the four things people forget to type before it:

      1. `adb forward tcp:8765 tcp:8765`   (USB only) - it is not persistent
      2. is the phone actually there and answering on that port?
      3. are the two things that must be installed by hand present (OBS Studio
         for the virtual camera, VB-CABLE for the virtual microphone)?
      4. are the three Python packages there, and is this PC's copy of
         phonebridge the one being run?

    Nothing is installed and no driver is touched: a missing piece is printed as
    an instruction. The bridge itself reconnects on its own - if the USB cable is
    pulled out and put back, the session is rebuilt and the camera, microphone and
    speaker are all asked for again. That is the point of running it from here
    rather than from three terminals.

.PARAMETER HostName
    Where the phone is. `127.0.0.1` (the default) means USB with an adb forward.
    Pass the phone's address for Wi-Fi or a hotspot, e.g. `-HostName 192.168.43.1`.

.PARAMETER Port
    The phone's control port. Default 8765, the same as `adb forward`.

.PARAMETER Profile
    Camera preset: `low` (640x480, the default here - it is the one that runs at
    a full 30 fps over USB), `720p`, or `high` (1080p).

.PARAMETER FpsMode
    `fixed` (default) pins the phone's auto-exposure to the requested frame rate,
    so 30 fps means 30 fps and a dim room is darker. `variable` lets the device
    slow down for a brighter picture.

.PARAMETER KeepAlive
    Send silence while this PC is not playing anything, so the phone's AudioTrack
    and the Bluetooth link stay warm. Costs ~24 KB/s and removes the re-buffering
    pause when playback resumes.

.PARAMETER Duration
    Stop after this many seconds. Default: until Ctrl+C.

.PARAMETER SkipChecks
    Run the bridge even if a check failed. The bridge's own error messages are
    better than this script's when something is genuinely missing.

.PARAMETER Python
    Python executable to use. Default: `.venv\Scripts\python.exe` in the repo,
    then `python` on PATH.

.EXAMPLE
    .\scripts\start-live.bat

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\start-live.ps1 -Profile 720p -KeepAlive

.EXAMPLE
    .\scripts\start-live.bat -HostName 192.168.43.1

.NOTES
    Exit codes: 0 the bridge ran and stopped cleanly, 1 something is missing (see
    the [fail] lines), 2 Python or the package is not usable, 3 the phone did not
    answer, 130 Ctrl+C.
#>
[CmdletBinding()]
param(
    [string]$HostName = '127.0.0.1',
    [int]$Port = 8765,
    [ValidateSet('low', '720p', 'high')][string]$Profile = 'low',
    [ValidateSet('fixed', 'variable')][string]$FpsMode = 'fixed',
    [switch]$KeepAlive,
    [double]$Duration = 0,
    [switch]$SkipChecks,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
# Probes run Python and *expect* failures ("that package is not installed"), and
# PowerShell 7.4+ can turn a native command's exit code into a terminating error.
# Switch that off and read $LASTEXITCODE by hand, like setup-windows.ps1 does.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$script:Failures = @()
$script:Warnings = @()

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  [warn] $Text" -ForegroundColor Yellow }
function Write-Bad  { param([string]$Text) Write-Host "  [fail] $Text" -ForegroundColor Red }
function Write-Hint { param([string]$Text) Write-Host "         $Text" -ForegroundColor DarkGray }

function Resolve-Python {
    if ($Python) {
        if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) { return $null }
        return @($Python)
    }
    # The repo's own virtualenv first: running a different interpreter than the
    # one setup-windows.ps1 installed into is the classic "it says sounddevice is
    # missing but it is installed" morning.
    $repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
    $venv = Join-Path $repo '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return @($venv) }
    foreach ($candidate in @(@('py', '-3'), @('python'))) {
        if (Get-Command $candidate[0] -ErrorAction SilentlyContinue) { return $candidate }
    }
    return $null
}

function Invoke-Phonebridge {
    param([string[]]$Arguments)
    & $script:PythonCommand @('-m', 'phonebridge') @Arguments
}

function Test-IsLoopback {
    return ($HostName -eq '127.0.0.1' -or $HostName -eq 'localhost' -or $HostName -eq '::1')
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
import sys
try:
    import sounddevice
except Exception:
    print("nosounddevice")
    sys.exit(0)
names = [str(d["name"]) for d in sounddevice.query_devices()]
hit = [n for n in names if "cable input" in n.lower()]
print("found:" + hit[0] if hit else "missing")
'@
    try {
        $output = & $script:PythonCommand @('-c', $probe) 2>$null
    } catch {
        return 'unknown'
    }
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    $line = ($output | Select-Object -Last 1)
    if ($null -eq $line) { return 'unknown' }
    if ($line -like 'found:*') { return $line.Substring(6) }
    if ($line -eq 'missing') { return $null }
    return 'unknown'
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
    $output = & $script:PythonCommand @('-c', $probe) 2>$null
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    return (($output | Select-Object -Last 1) -as [string])
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host 'PhoneBridge - daily use' -ForegroundColor White
Write-Host '--------------------------------------------------------------'
Write-Host ''

Write-Step 'locating Python'
$script:PythonCommand = Resolve-Python
if (-not $script:PythonCommand) {
    Write-Bad 'Python 3.10+ was not found (and there is no .venv in this repo).'
    Write-Hint 'install Python from https://www.python.org/downloads/windows/ then run'
    Write-Hint '  powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1'
    exit 2
}
$version = (& $script:PythonCommand @('-c', "import sys; print('%d.%d.%d' % sys.version_info[:3])")).Trim()
$moduleOk = $true
& $script:PythonCommand @('-c', 'import phonebridge') 2>$null
if ($LASTEXITCODE -ne 0) { $moduleOk = $false }
if (-not $moduleOk) {
    Write-Bad "Python $version has no phonebridge package installed"
    Write-Hint 'from the repo root: python -m pip install -e .\windows'
    Write-Hint '(or run this script from the repo and let it find .venv)'
    exit 2
}
Write-Ok "Python $version ($($script:PythonCommand -join ' ')) - phonebridge importable"
Write-Host ''

if (-not $SkipChecks) {
    Write-Step 'USB (adb forward)'
    if (Test-IsLoopback) {
        $adb = Get-Command adb -ErrorAction SilentlyContinue
        if (-not $adb) {
            Write-Bad 'adb was not found on PATH, and 127.0.0.1 only works through an adb forward'
            Write-Hint 'install Android Platform Tools (or use -HostName <phone-ip> over Wi-Fi)'
            $script:Failures += 'adb'
        } else {
            $devices = @(& adb devices 2>$null | Select-Object -Skip 1 | Where-Object { $_ -match '\sdevice$' })
            if ($devices.Count -eq 0) {
                Write-Bad 'no USB device is listed by adb'
                Write-Hint 'plug the phone in, unlock it, and allow "USB debugging" when it asks'
                Write-Hint 'check with:  adb devices'
                $script:Failures += 'no adb device'
            } else {
                Write-Ok "adb sees $($devices.Count) device(s)"
                # `adb forward` is idempotent for the same port pair, and it does
                # NOT survive an unplug - which is exactly why it is re-run here
                # every morning rather than once at setup.
                $forward = (& adb forward "tcp:$Port" "tcp:$Port" 2>&1)
                if ($LASTEXITCODE -ne 0) {
                    Write-Bad "adb forward tcp:$Port tcp:$Port failed: $forward"
                    $script:Failures += 'adb forward'
                } else {
                    Write-Ok "adb forward tcp:$Port tcp:$Port"
                }
            }
        }
    } else {
        Write-Ok "Wi-Fi/hotspot target $HostName - no adb needed"
        Write-Hint 'the phone must be listening (open PhoneBridge on it), and both devices on the same network'
    }
    Write-Host ''

    Write-Step 'OBS Studio (the "OBS Virtual Camera" device)'
    $obs = Test-ObsInstalled
    if ($obs) {
        Write-Ok "OBS found: $obs"
        Write-Hint 'open OBS once per boot: it registers the virtual camera device'
    } else {
        Write-Warn 'OBS Studio is not installed, so there is no virtual camera to feed'
        Write-Hint 'install from https://obsproject.com/download (or Unity Capture as a fallback)'
        $script:Warnings += 'OBS'
    }
    Write-Host ''

    Write-Step 'VB-CABLE (the "CABLE Output" microphone)'
    $cable = Test-CableInstalled
    if ($cable -and $cable -ne 'unknown') {
        Write-Ok "audio device found: $cable"
    } elseif ($cable -eq 'unknown') {
        Write-Warn 'could not list audio devices (is sounddevice installed? see below)'
        $script:Warnings += 'VB-CABLE'
    } else {
        Write-Warn 'VB-CABLE is not installed, so the phone microphone has nowhere to go'
        Write-Hint 'install from https://vb-audio.com/Cable/ then REBOOT, and run setup-windows.ps1'
        $script:Warnings += 'VB-CABLE'
    }
    Write-Host ''

    Write-Step 'Python packages for the live paths'
    $missing = Test-PythonPackages
    if ($missing -eq 'unknown') {
        Write-Warn 'could not check the packages'
    } elseif ($missing) {
        Write-Bad "missing: $missing"
        Write-Hint "fix with: $($script:PythonCommand -join ' ') -m pip install $($missing -replace ',', ' ')"
        $script:Failures += 'python packages'
    } else {
        Write-Ok 'pyvirtualcam, sounddevice and PyAudioWPatch are all importable'
    }
    Write-Host ''

    Write-Step "phone on ${HostName}:$Port"
    $info = Invoke-Phonebridge @('info', '--host', $HostName, '--port', "$Port", '--format', 'json')
    if ($LASTEXITCODE -ne 0) {
        Write-Bad 'the phone did not answer'
        Write-Hint 'is PhoneBridge open on the phone, and is the port forwarded (USB) or reachable (Wi-Fi)?'
        Write-Hint 'the first connection needs a pairing code compared on both screens'
        $script:Failures += 'phone unreachable'
    } else {
        Write-Ok 'the phone answered'
    }
    Write-Host ''
}

if ($script:Failures.Count -gt 0 -and -not $SkipChecks) {
    Write-Host 'Not starting: fix the [fail] lines above, or re-run with -SkipChecks.' -ForegroundColor Red
    Write-Host 'The bridge would fail too, with the same reasons - this is the short list.' -ForegroundColor DarkGray
    exit 1
}

# --------------------------------------------------------------------------- #
$arguments = @(
    'start',
    '--camera', '--mic', '--speaker',
    '--host', $HostName,
    '--port', "$Port",
    '--profile', $Profile,
    '--fps-mode', $FpsMode
)
if ($KeepAlive) { $arguments += '--keep-alive' }
if ($Duration -gt 0) { $arguments += @('--duration', "$Duration") }

Write-Step "starting: camera + microphone + speaker"
Write-Hint ("phonebridge " + ($arguments -join ' '))
Write-Hint 'a dropped link reconnects on its own: pull the USB cable out and back, it comes back'
Write-Hint 'in the other app: camera = "OBS Virtual Camera", microphone = "CABLE Output"'
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
    # 130 from bash-style tooling, 0xC000013A from Windows' Ctrl+C.
    Write-Ok 'stopped (Ctrl+C)'
    $code = 0
} else {
    Write-Bad "the bridge stopped with exit code $code"
    Write-Hint 'the last status line above is the phone''s own answer; it says what failed'
}
exit $code
