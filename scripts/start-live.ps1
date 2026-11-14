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
    [string]$Python,
    # Which output the speaker path captures, as a name substring. Empty means the
    # default output - which is also the speakers you are listening to, so the PC
    # keeps playing. A second virtual cable is what makes the capture private.
    [string]$LoopbackDevice = ''
)

$ErrorActionPreference = 'Stop'
# Probes run Python and *expect* failures ("that package is not installed"), and
# PowerShell 7.4+ can turn a native command's exit code into a terminating error.
# Switch that off and read $LASTEXITCODE by hand, like setup-windows.ps1 does.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# Anything that escapes the checks below is a bug in *this script*, and a raw
# PowerShell stack trace is not a useful thing to hand somebody at 9 a.m. Say what
# happened, say it is the launcher's fault, and print the command that works.
trap {
    Write-Host ''
    Write-Host '  the launcher hit an unexpected error:' -ForegroundColor Red
    Write-Host "    $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '  that is a bug in start-live.ps1, not in the bridge. Start it yourself:' -ForegroundColor DarkGray
    Write-Host "    python -m phonebridge start --camera --mic --speaker --host $HostName --port $Port" -ForegroundColor DarkGray
    exit 1
}

$script:Failures = @()
$script:Warnings = @()

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  [warn] $Text" -ForegroundColor Yellow }
function Write-Bad  { param([string]$Text) Write-Host "  [fail] $Text" -ForegroundColor Red }
function Write-Hint { param([string]$Text) Write-Host "         $Text" -ForegroundColor DarkGray }

function Get-ScriptDirectory {
    # `$MyInvocation.MyCommand.Path` is null inside a *function* - a function has no
    # path - and `Split-Path -Parent $null` throws "Cannot bind argument to
    # parameter 'Path' because it is null" before any useful fallback runs. That is
    # exactly how this script failed the first time it was run. $PSScriptRoot is the
    # script's own folder and needs no call at all; the other two are fallbacks for
    # `powershell -File` in odd hosts, guarded rather than assumed.
    if ($PSScriptRoot) { return $PSScriptRoot }
    if ($MyInvocation.MyCommand.Path) { return (Split-Path -Parent $MyInvocation.MyCommand.Path) }
    return (Get-Location).ProviderPath
}

$script:RepoRoot = Split-Path -Parent (Get-ScriptDirectory)

function Invoke-PythonCandidate {
    # A candidate is an *array*: `py -3` is a launcher plus an argument, and Python
    # 3.14 from python.org is a path. Slicing with `$Command[1..($Command.Count-1)]`
    # silently turns a one-element command into `python.exe <itself>` (PowerShell
    # ranges count *down*), so the tail is built by hand.
    param([string[]]$Command, [string[]]$Arguments)
    $exe = $Command[0]
    $prefix = @()
    if ($Command.Count -gt 1) {
        $prefix = @($Command | Select-Object -Skip 1)
    }
    return (& $exe @prefix @Arguments 2>$null)
}

function Test-Interpreter {
    # A candidate is only a candidate: it has to run, and it has to be Python 3.10+.
    # `py -3` and the Store alias for `python` both exist on PATH without being a
    # usable interpreter, which is why "Get-Command python" was never enough.
    param([string[]]$Command)
    try {
        $output = Invoke-PythonCandidate -Command $Command -Arguments @(
            '-c', 'import sys; print(sys.version_info[0], sys.version_info[1])'
        )
    } catch {
        return $null
    }
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
    } catch {
        return $false
    }
    return ($LASTEXITCODE -eq 0)
}

function Get-PythonCandidates {
    # Ordered by how likely each one is to be the interpreter setup-windows.ps1
    # installed into. The repo's virtualenv wins, then the launcher `py`, then
    # every python on PATH, then the per-user installs under AppData - Python 3.14
    # from python.org installs to %LOCALAPPDATA%\Programs\Python\Python314 and is
    # on PATH for the *user* only, which is easy to miss from an elevated shell.
    $candidates = @()
    if ($Python) { $candidates += , @($Python) }
    foreach ($venv in @('.venv', 'venv')) {
        $exe = Join-Path (Join-Path $script:RepoRoot $venv) 'Scripts\python.exe'
        if (Test-Path $exe) { $candidates += , @($exe) }
    }
    foreach ($name in @(@('py', '-3'), @('py'), @('python'), @('python3'))) {
        if (Get-Command $name[0] -ErrorAction SilentlyContinue) { $candidates += , $name }
    }
    # Per-user installs: Python 3.14 from python.org lands in
    # %LOCALAPPDATA%\Programs\Python\Python314 and is only on the *user* PATH,
    # which an elevated shell does not inherit. Both roots are checked, and only
    # when the variable actually holds a path.
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Programs\Python') }
    if ($env:ProgramFiles) { $roots += $env:ProgramFiles }
    foreach ($root in $roots) {
        # Guarded, not assumed: Join-Path and Get-ChildItem both refuse a null path,
        # and a thrown binding error here is the same class of crash as the one that
        # stopped the script before it started.
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
    # Returns @{ Command = @(...); Version = '3.x'; HasPackage = $true/$false } for
    # the first interpreter that runs *and* can import phonebridge. A working
    # interpreter without the package is remembered, so the message can say
    # "Python 3.14 is here, it just does not have phonebridge installed" instead of
    # the useless "Python was not found".
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
$resolved = Resolve-Python
if (-not $resolved) {
    Write-Bad 'no usable Python was found (this needs Python 3.10 or newer).'
    Write-Hint 'looked at: .venv\Scripts\python.exe, venv\Scripts\python.exe, py -3, py, python,'
    Write-Hint 'python3, %LOCALAPPDATA%\Programs\Python\Python3*\python.exe, %ProgramFiles%\Python3*'
    Write-Hint 'install it from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH")'
    Write-Hint 'then run: powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1'
    Write-Hint 'or point this script at one directly: start-live.bat -Python C:\Python311\python.exe'
    exit 2
}
$script:PythonCommand = $resolved.Command
if (-not $resolved.HasPackage) {
    Write-Bad "Python $($resolved.Version) at $($script:PythonCommand -join ' ') cannot import phonebridge"
    Write-Hint 'the interpreter works, the package is not installed into it. From the repo root:'
    Write-Hint "  $($script:PythonCommand -join ' ') -m pip install -e .\windows"
    Write-Hint 'or run setup-windows.ps1, which installs into a .venv this script prefers.'
    exit 2
}
Write-Ok "Python $($resolved.Version) ($($script:PythonCommand -join ' ')) - phonebridge importable"
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
    # The speaker path captures an *output*, and by default that is the one you are
    # listening to - so the PC keeps playing and muting it kills the phone's audio.
    # A second cable is the fix, and it is worth saying here rather than in a doc.
    if ($LoopbackDevice) {
        Write-Ok "speaker captures: $LoopbackDevice"
    } elseif ($cable -and $cable -ne 'unknown') {
        Write-Hint 'speaker captures the default output unless you pass -LoopbackDevice:'
        Write-Hint '  phonebridge speaker --list-loopback-devices   (a second VB-CABLE keeps the room quiet)'
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
if ($LoopbackDevice) { $arguments += @('--loopback-device', $LoopbackDevice) }
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
