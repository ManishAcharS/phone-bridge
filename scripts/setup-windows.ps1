<#
.SYNOPSIS
    Prepare this PC for `phonebridge camera|mic|speaker`.

.DESCRIPTION
    Installs the three Python packages the live delivery paths need, then checks
    for the two Windows components that must be installed by hand because they are
    signed drivers / applications, not Python packages:

        OBS Studio        provides the "OBS Virtual Camera" device
        VB-CABLE          provides "CABLE Input" and the "CABLE Output" microphone

    Nothing here installs a driver, changes a security setting, or needs
    administrator rights. If a component is missing, this script says so and
    prints the download page; it never downloads or runs an installer itself.

.PARAMETER Check
    Report only. Installs nothing, changes nothing.

.PARAMETER Python
    Python executable to use. Default: `py -3`, then `python` from PATH.

.PARAMETER Upgrade
    Allow pip to upgrade already-installed packages.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -Check

.NOTES
    Exit codes: 0 everything present, 1 a component still needs installing,
                2 Python itself is missing or too old.
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Upgrade,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
# Several probes below run Python and *expect* a non-zero exit ("that package is
# not installed"). On PowerShell 7.4+ a native command's exit code can be turned
# into a terminating error, which would kill the script instead of reporting, so
# switch that off and keep reading $LASTEXITCODE by hand.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$script:Missing = @()

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Need { param([string]$Text) Write-Host "  [todo] $Text" -ForegroundColor Yellow }
function Write-Bad  { param([string]$Text) Write-Host "  [fail] $Text" -ForegroundColor Red }

function Resolve-Python {
    if ($Python) {
        if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
            throw "Python executable not found: $Python"
        }
        return $Python
    }
    foreach ($candidate in @('py', 'python')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -eq $command) { continue }
        try {
            if ($candidate -eq 'py') {
                $version = & py -3 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($LASTEXITCODE -eq 0) { return @('py', '-3') }
                continue
            }
            $version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0) { return @('python') }
        } catch {
            continue
        }
    }
    return $null
}

function Invoke-Python {
    param([string[]]$Arguments)
    # Select-Object -Skip 1, not [$Command[1..($Command.Count - 1)]]: for a
    # one-element array that slice counts *down* to zero, so `python` was invoked
    # as `python.exe <path to python>` and every probe failed.
    if ($script:PythonCommand -is [array]) {
        $exe = [string]$script:PythonCommand[0]
        $rest = @($script:PythonCommand | Select-Object -Skip 1)
        & $exe @rest @Arguments
    } else {
        & $script:PythonCommand @Arguments
    }
}

function Get-PackageVersion {
    param([string]$Name)
    $probe = "import importlib.metadata as m; print(m.version('$Name'))"
    try {
        $output = Invoke-Python @('-c', $probe) 2>$null
    } catch {
        return $null  # not installed: the import failed, which is the answer
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($output | Select-Object -First 1).Trim()
}

function Ensure-Package {
    param([string]$Name, [string]$Why)
    $version = Get-PackageVersion $Name
    if ($version) {
        Write-Ok "$Name $version - $Why"
        if ($Upgrade -and -not $Check) {
            Invoke-Python @('-m', 'pip', 'install', '--quiet', '--upgrade', $Name) | Out-Null
        }
        return
    }
    if ($Check) {
        Write-Need "$Name is not installed - $Why"
        $script:Missing += "$Name (pip install $Name)"
        return
    }
    Write-Step "installing $Name"
    Invoke-Python @('-m', 'pip', 'install', '--quiet', $Name) | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Bad "$Name could not be installed; run: python -m pip install $Name"
        $script:Missing += "$Name (pip install $Name)"
        return
    }
    $version = Get-PackageVersion $Name
    Write-Ok "$Name $version installed - $Why"
}

function Test-ObsInstalled {
    $paths = @(
        'C:\Program Files\OBS Studio\bin\64bit\obs64.exe',
        'C:\Program Files (x86)\OBS Studio\bin\64bit\obs64.exe'
    )
    foreach ($path in $paths) {
        if (Test-Path $path) { return $path }
    }
    # A portable OBS install has no registry entry, so also ask the registry.
    $keys = @(
        'HKLM:\SOFTWARE\OBS Studio',
        'HKCU:\SOFTWARE\OBS Studio'
    )
    foreach ($key in $keys) {
        if (Test-Path $key) { return $key }
    }
    return $null
}

function Get-LoopbackDevices {
    # Every endpoint WASAPI loopback can capture, with the same probe the speaker
    # command uses. Two of them is the good case: the microphone writes into one
    # (VB-CABLE's "CABLE Input") and the speaker path captures the *other*, so the
    # phone hears the PC without the PC's speakers having to be audible, and
    # without capturing its own microphone.
    $probe = @'
import sys
names = []
try:
    import pyaudiowpatch as pyaudio
    pa = pyaudio.PyAudio()
    try:
        for info in pa.get_loopback_device_info_generator():
            if int(info.get("maxInputChannels", 0)) > 0:
                names.append(str(info.get("name", "")))
    finally:
        pa.terminate()
except Exception:
    try:
        import soundcard
        names = [str(m) for m in soundcard.all_microphones(include_loopback=True)]
    except Exception:
        print("unknown")
        sys.exit(0)
print("|".join(names))
'@
    try {
        $output = Invoke-Python @('-c', $probe) 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    $line = ($output | Select-Object -Last 1)
    if ($null -eq $line -or $line -eq 'unknown') { return $null }
    return @($line -split '\|' | Where-Object { $_ })
}

function Test-CableInstalled {
    # sounddevice just got installed, so ask PortAudio directly: this is the same
    # lookup phonebridge mic --to-cable performs, which makes it the honest check.
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
        $output = Invoke-Python @('-c', $probe) 2>$null
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

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host 'PhoneBridge - Windows live delivery setup' -ForegroundColor White
Write-Host '------------------------------------------'
if ($Check) { Write-Host 'check only: nothing will be installed' -ForegroundColor DarkGray }
Write-Host ''

Write-Step 'locating Python'
$script:PythonCommand = Resolve-Python
if (-not $script:PythonCommand) {
    Write-Bad 'Python 3.10+ was not found. Install it from https://www.python.org/downloads/windows/'
    exit 2
}
$version = (Invoke-Python @('-c', "import sys; print('%d.%d.%d' % sys.version_info[:3])")).Trim()
Write-Ok "Python $version ($($script:PythonCommand -join ' '))"
Write-Host ''

Write-Step 'Python packages (installed by pip, no administrator rights needed)'
Ensure-Package 'pyvirtualcam'   'virtual webcam output (needs OBS Studio for the device itself)'
Ensure-Package 'sounddevice'    'writes the phone microphone into VB-CABLE'
Ensure-Package 'PyAudioWPatch'  'WASAPI loopback capture of what the PC is playing'
Write-Host ''

Write-Step 'OBS Studio (provides the "OBS Virtual Camera" device)'
$obs = Test-ObsInstalled
if ($obs) {
    Write-Ok "OBS found: $obs"
} else {
    Write-Need 'OBS Studio is not installed - the camera path has no device without it'
    Write-Host '         install from https://obsproject.com/download (then open OBS once, so it'
    Write-Host '         registers its virtual camera), or install Unity Capture as a fallback'
    $script:Missing += 'OBS Studio (https://obsproject.com/download)'
}
Write-Host ''

Write-Step 'VB-CABLE (provides "CABLE Input" / "CABLE Output")'
$cable = Test-CableInstalled
if ($cable -and $cable -ne 'unknown') {
    Write-Ok "audio device found: $cable"
} elseif ($cable -eq 'unknown') {
    Write-Need 'could not list audio devices yet (reboot after installing VB-CABLE, then re-run)'
} else {
    Write-Need 'VB-CABLE is not installed - the microphone path has nowhere to write'
    Write-Host '         install from https://vb-audio.com/Cable/ then REBOOT (the driver only'
    Write-Host '         appears after a restart), and re-run this script'
    $script:Missing += 'VB-CABLE (https://vb-audio.com/Cable/) - reboot after installing'
}
Write-Host ''

Write-Step 'a second virtual cable (speaker path: keep the microphone's cable free)'
$loopbacks = Get-LoopbackDevices
if ($null -eq $loopbacks) {
    Write-Need 'could not list capture devices yet (install PyAudioWPatch, then re-run)'
} else {
    $virtuals = @($loopbacks | Where-Object { $_ -match 'cable|voicemeeter|virtual audio' })
    Write-Host "         capture devices visible: $($loopbacks.Count)" -ForegroundColor DarkGray
    foreach ($name in $virtuals) { Write-Host "           $name" -ForegroundColor DarkGray }
    if ($virtuals.Count -ge 2) {
        Write-Ok "$($virtuals.Count) virtual cables: keep the microphone on one, capture the other"
        Write-Host '         next: phonebridge speaker --list-loopback-devices, then' -ForegroundColor DarkGray
        Write-Host '               phonebridge speaker --loopback-device "CABLE-A Input"' -ForegroundColor DarkGray
    } elseif ($virtuals.Count -eq 1) {
        Write-Need 'one virtual cable: that is the microphone path'
        Write-Host '         capturing it too would feed the phone its own microphone. For a' -ForegroundColor DarkGray
        Write-Host '         silent PC-audio capture, install VB-CABLE A+B (or VoiceMeeter) and' -ForegroundColor DarkGray
        Write-Host '         point the speaker path at the second cable:' -ForegroundColor DarkGray
        Write-Host '           https://vb-audio.com/Cable/' -ForegroundColor DarkGray
        $script:Missing += 'a second virtual cable for the speaker path (https://vb-audio.com/Cable/)'
    } else {
        Write-Need 'no virtual cable: the speaker path can only capture an output that also plays out loud'
        Write-Host '         install VB-CABLE (A+B for two cables): https://vb-audio.com/Cable/' -ForegroundColor DarkGray
        $script:Missing += 'VB-CABLE for the speaker path (https://vb-audio.com/Cable/)'
    }
}
Write-Host ''

Write-Step 'summary'
if ($script:Missing.Count -eq 0) {
    Write-Ok 'everything the three live commands need is present'
    Write-Host ''
    Write-Host '  next:  phonebridge camera --virtual' -ForegroundColor DarkGray
    Write-Host '         phonebridge mic --to-cable' -ForegroundColor DarkGray
    Write-Host '         phonebridge speaker' -ForegroundColor DarkGray
    exit 0
}

Write-Need 'still to do by hand:'
foreach ($item in $script:Missing) { Write-Host "         - $item" }
Write-Host ''
Write-Host 'Nothing here installs a driver automatically: both remaining items are' -ForegroundColor DarkGray
Write-Host 'signed installers that need a human to read and accept them.' -ForegroundColor DarkGray
exit 1
