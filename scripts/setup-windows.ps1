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
    Optional Python executable path. Leave empty for automatic selection; the
    Windows py launcher is invoked as executable py with separate argument -3.

.PARAMETER Upgrade
    Allow pip to upgrade already-installed packages.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -Check

.NOTES
    Exit codes: 0 everything present (or only inconclusive warnings), 1 a
                component is positively missing, 2 Python itself is missing or
                too old. A check that cannot run prints the child exit code and
                stderr as a warning and does not block the summary.
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
if (-not $PSScriptRoot) { throw 'Run this script from its file path, not from a pasted block.' }
$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:Missing = @()

$commandHelpers = Join-Path $PSScriptRoot 'python-command.ps1'
if (-not (Test-Path -LiteralPath $commandHelpers)) { throw "Python command helpers not found: $commandHelpers" }
. $commandHelpers
$probeHelpers = Join-Path $PSScriptRoot 'python-probes.ps1'
if (-not (Test-Path -LiteralPath $probeHelpers)) { throw "Python probe helpers not found: $probeHelpers" }
. $probeHelpers

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Need { param([string]$Text) Write-Host "  [todo] $Text" -ForegroundColor Yellow }
function Write-Warn { param([string]$Text) Write-Host "  [warn] $Text" -ForegroundColor Yellow }
function Write-Bad  { param([string]$Text) Write-Host "  [fail] $Text" -ForegroundColor Red }
function Write-Hint { param([string]$Text) Write-Host "         $Text" -ForegroundColor DarkGray }

function Get-PackageState {
    param([string]$Name)
    $probe = @"
import importlib.metadata as m
try:
    print(m.version('$Name'))
except m.PackageNotFoundError:
    print('missing')
"@
    $result = Invoke-PythonProbe -Probe $probe
    if ($result.ExitCode -ne 0 -or $null -eq $result.Output) {
        return [pscustomobject]@{ Status = 'unknown'; Version = ''; Detail = (Format-ProbeUnknown -Result $result) }
    }
    $line = ([string]$result.Output).Trim()
    if ($line -eq 'missing') {
        return [pscustomobject]@{ Status = 'missing'; Version = ''; Detail = '' }
    }
    if (-not $line) {
        return [pscustomobject]@{ Status = 'unknown'; Version = ''; Detail = (Format-ProbeUnknown -Result $result) }
    }
    return [pscustomobject]@{ Status = 'ok'; Version = $line; Detail = '' }
}

function Ensure-Package {
    param([string]$Name, [string]$Why)
    $state = Get-PackageState -Name $Name
    if ($state.Status -eq 'unknown') {
        Write-Warn "$Name check could not run: $($state.Detail)"
        Write-Hint 'continuing; the live command will report its own package/device error'
        return
    }
    if ($state.Status -eq 'ok') {
        Write-Ok "$Name $($state.Version) - $Why"
        if ($Upgrade -and -not $Check) {
            Invoke-PythonCommand -Executable $script:PythonExe -Prefix $script:PythonArgs -Arguments @('-m', 'pip', 'install', '--quiet', '--upgrade', $Name) | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Bad "$Name upgrade failed with exit code $LASTEXITCODE; run: $(Format-PythonCommand -Executable $script:PythonExe -Arguments ($script:PythonArgs + @('-m', 'pip', 'install', '--upgrade', $Name)))"
                $script:Missing += "$Name upgrade failed (pip install --upgrade $Name)"
            }
        }
        return
    }
    if ($Check) {
        Write-Need "$Name is not installed - $Why"
        $script:Missing += "$Name (pip install $Name)"
        return
    }
    Write-Step "installing $Name"
    Invoke-PythonCommand -Executable $script:PythonExe -Prefix $script:PythonArgs -Arguments @('-m', 'pip', 'install', '--quiet', $Name) | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Bad "$Name could not be installed (exit $LASTEXITCODE); run: $(Format-PythonCommand -Executable $script:PythonExe -Arguments ($script:PythonArgs + @('-m', 'pip', 'install', $Name)))"
        $script:Missing += "$Name (pip install $Name)"
        return
    }
    $state = Get-PackageState -Name $Name
    if ($state.Status -eq 'ok') {
        Write-Ok "$Name $($state.Version) installed - $Why"
    } elseif ($state.Status -eq 'unknown') {
        Write-Warn "$Name was installed but the version check could not run: $($state.Detail)"
    } else {
        Write-Bad "$Name still is not importable after pip install"
        $script:Missing += "$Name (pip install $Name)"
    }
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

function Get-LoopbackState {
    # Every endpoint WASAPI loopback can capture, with the same probe the speaker
    # command uses. Two of them is the good case: the microphone writes into one
    # (VB-CABLE's "CABLE Input") and the speaker path captures the *other*, so the
    # phone hears the PC without the PC's speakers having to be audible, and
    # without capturing its own microphone.
    $probe = @'
import pyaudiowpatch as pyaudio
names = []
pa = pyaudio.PyAudio()
try:
    for info in pa.get_loopback_device_info_generator():
        if int(info.get("maxInputChannels", 0)) > 0:
            names.append(str(info.get("name", "")))
finally:
    pa.terminate()
print("|".join(names))
'@
    $result = Invoke-PythonProbe -Probe $probe
    if ($result.ExitCode -ne 0 -or $null -eq $result.Output) {
        return [pscustomobject]@{ Status = 'unknown'; Devices = @(); Detail = (Format-ProbeUnknown -Result $result) }
    }
    $line = ([string]$result.Output).Trim()
    $devices = @($line -split '\|' | Where-Object { $_ })
    return [pscustomobject]@{ Status = 'ok'; Devices = $devices; Detail = '' }
}

function Test-CableInstalled {
    # sounddevice just got installed, so ask PortAudio directly: this is the same
    # lookup phonebridge mic --to-cable performs, which makes it the honest check.
    $probe = @'
import sounddevice
names = [str(d["name"]) for d in sounddevice.query_devices()]
hit = [n for n in names if "cable input" in n.lower()]
print("found:" + hit[0] if hit else "missing")
'@
    $result = Invoke-PythonProbe -Probe $probe
    if ($result.ExitCode -ne 0 -or $null -eq $result.Output) {
        return Format-ProbeUnknown -Result $result
    }
    $line = ([string]$result.Output).Trim()
    if ($line -like 'found:*') { return $line.Substring(6) }
    if ($line -eq 'missing') { return $null }
    return Format-ProbeUnknown -Result $result
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host 'PhoneBridge - Windows live delivery setup' -ForegroundColor White
Write-Host '------------------------------------------'
if ($Check) { Write-Host 'check only: nothing will be installed' -ForegroundColor DarkGray }
Write-Host ''

Write-Step 'locating Python'
$resolved = Resolve-Python -RepoRoot $script:RepoRoot -PythonOverride $Python
if (-not $resolved) {
    Write-Bad 'Python 3.10+ was not found. Install it from https://www.python.org/downloads/windows/'
    exit 2
}
$script:PythonExe = $resolved.Exe
$script:PythonArgs = @($resolved.Args)
Write-Ok "Python $($resolved.Version) ($(Format-PythonCommand -Executable $script:PythonExe -Arguments $script:PythonArgs))"
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
if ($cable -like 'unknown*') {
    Write-Warn "VB-CABLE check could not run: $cable"
    Write-Hint 'continuing; phonebridge mic will report its own VB-CABLE/device error'
} elseif ($cable) {
    Write-Ok "audio device found: $cable"
} else {
    Write-Need 'VB-CABLE is not installed - the microphone path has nowhere to write'
    Write-Host '         install from https://vb-audio.com/Cable/ then REBOOT (the driver only'
    Write-Host '         appears after a restart), and re-run this script'
    $script:Missing += 'VB-CABLE (https://vb-audio.com/Cable/) - reboot after installing'
}
Write-Host ''

Write-Step 'a second virtual cable (the speaker path must not capture the microphone one)'
$loopbacks = Get-LoopbackState
if ($loopbacks.Status -eq 'unknown') {
    Write-Warn "speaker capture check could not run: $($loopbacks.Detail)"
    Write-Hint 'continuing; phonebridge speaker will report its own loopback/device error'
} else {
    $virtuals = @($loopbacks.Devices | Where-Object { $_ -match 'cable|voicemeeter|virtual audio' })
    Write-Host "         capture devices visible: $($loopbacks.Devices.Count)" -ForegroundColor DarkGray
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
    Write-Ok 'everything the three live commands need is present (or only warnings above)'
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
