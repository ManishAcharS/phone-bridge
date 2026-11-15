# Python probe helpers for start-live.ps1.
#
# Windows PowerShell 5.1 passes native arguments through a legacy quoting path.
# A multiline `python -c` here-string with embedded double quotes can arrive at
# Python with those quotes stripped, so the child exits with SyntaxError while
# the launcher only sees a nonzero exit. These helpers send the probe (and any
# probe arguments) as base64 and execute a one-line bootstrap whose only quoted
# literals are base64 text and 'utf-8'. They also capture stdout, stderr and the
# real exit code instead of hiding them.
#
# Expected scope: the caller dot-sources this file and sets $script:PythonCommand
# to a command array, e.g. @('py', '-3') or @('C:\...\python.exe').

function Invoke-PythonProbe {
    param(
        [string]$Probe,
        [string[]]$ProbeArguments = @()
    )
    if (-not $script:PythonCommand -or $script:PythonCommand.Count -eq 0) {
        return @{ ExitCode = -1; StdOut = ''; StdErr = 'no Python command selected'; Output = $null }
    }

    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Probe))
    $argumentBootstrap = ''
    $encodedArguments = @()
    if ($ProbeArguments.Count -gt 0) {
        # Device names can contain spaces or quotes; base64 keeps them out of the
        # legacy native argument quoting path too.
        $argumentBootstrap = "import sys;sys.argv=[sys.argv[0]]+[base64.b64decode(a).decode('utf-8') for a in sys.argv[1:]];"
        foreach ($argument in $ProbeArguments) {
            $encodedArguments += [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($argument))
        }
    }
    # The bootstrap contains no embedded double quotes and no newlines. The probe
    # itself may contain any Python punctuation because it travels as base64.
    $bootstrap = "import base64;" + $argumentBootstrap + "exec(base64.b64decode('$encoded').decode('utf-8'))"
    $arguments = @('-c', $bootstrap)
    if ($encodedArguments.Count -gt 0) { $arguments += $encodedArguments }

    $previousPreference = $ErrorActionPreference
    $hadEncoding = Test-Path Env:\PYTHONIOENCODING
    $previousEncoding = $env:PYTHONIOENCODING
    $previousConsoleEncoding = [Console]::OutputEncoding
    $ErrorActionPreference = 'Continue'
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $exe = $script:PythonCommand[0]
        $prefix = @()
        if ($script:PythonCommand.Count -gt 1) {
            $prefix = @($script:PythonCommand | Select-Object -Skip 1)
        }
        $records = & $exe @prefix @arguments 2>&1
        $exit = $LASTEXITCODE
        $stdoutLines = @()
        $stderrLines = @()
        foreach ($record in @($records)) {
            if ($record -is [System.Management.Automation.ErrorRecord]) {
                $stderrLines += [string]$record.Exception.Message
            } else {
                $stdoutLines += [string]$record
            }
        }
        $stdout = ($stdoutLines -join "`n").Trim()
        $stderr = ($stderrLines -join "`n").Trim()
        $last = $null
        if ($stdoutLines.Count -gt 0) { $last = ($stdoutLines | Select-Object -Last 1) }
        return @{ ExitCode = $exit; StdOut = $stdout; StdErr = $stderr; Output = $last }
    } catch {
        return @{ ExitCode = -1; StdOut = ''; StdErr = $_.Exception.Message; Output = $null }
    } finally {
        $ErrorActionPreference = $previousPreference
        [Console]::OutputEncoding = $previousConsoleEncoding
        if ($hadEncoding) {
            $env:PYTHONIOENCODING = $previousEncoding
        } else {
            Remove-Item Env:\PYTHONIOENCODING -ErrorAction SilentlyContinue
        }
    }
}

function Format-ProbeUnknown {
    param($Result)
    $exit = $Result.ExitCode
    $stderr = [string]$Result.StdErr
    $stdout = [string]$Result.StdOut
    if ([string]::IsNullOrWhiteSpace($stderr)) { $stderr = '<empty>' }
    if ([string]::IsNullOrWhiteSpace($stdout)) { $stdout = '<empty>' }
    return "unknown: exit=$exit; stderr=$stderr; stdout=$stdout"
}

function Test-PythonPackages {
    $probe = @'
import importlib
names = ("pyvirtualcam", "sounddevice", "pyaudiowpatch")
missing = []
for name in names:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        missing.append(name)
print(",".join(missing) if missing else "ok")
'@
    $result = Invoke-PythonProbe -Probe $probe
    if ($result.ExitCode -ne 0 -or $null -eq $result.Output) {
        return Format-ProbeUnknown -Result $result
    }
    $line = ([string]$result.Output).Trim()
    if ($line -eq 'ok') { return 'ok' }
    $parts = @($line.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
    $allowed = @('pyvirtualcam', 'sounddevice', 'pyaudiowpatch')
    foreach ($part in $parts) {
        if ($allowed -notcontains $part) {
            return Format-ProbeUnknown -Result $result
        }
    }
    if ($parts.Count -eq 0) { return Format-ProbeUnknown -Result $result }
    return ($parts -join ',')
}

function Test-CableInstalled {
    $probe = @'
import sounddevice
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
    $result = Invoke-PythonProbe -Probe $probe
    if ($result.ExitCode -ne 0 -or $null -eq $result.Output) {
        return Format-ProbeUnknown -Result $result
    }
    $line = ([string]$result.Output).Trim()
    if ($line -like 'found:*' -or $line -eq 'partial' -or $line -eq 'missing') {
        return $line
    }
    return Format-ProbeUnknown -Result $result
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
    $result = Invoke-PythonProbe -Probe $probe -ProbeArguments @($Name)
    if ($result.ExitCode -ne 0 -or $null -eq $result.Output) {
        return Format-ProbeUnknown -Result $result
    }
    $line = ([string]$result.Output).Trim()
    if ($line -like 'found:*' -or $line -eq 'missing') {
        return $line
    }
    return Format-ProbeUnknown -Result $result
}
