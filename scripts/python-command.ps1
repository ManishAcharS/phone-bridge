# Shared Python invocation/discovery helpers for the Windows PowerShell scripts.
#
# The executable and its launcher arguments are always separate values. A command
# such as `py -3` is represented as Executable='py', Prefix=@('-3'), never as the
# single string 'py -3'. PowerShell's call operator treats a single string as one
# command name, so a combined executable-plus-argument string fails even when
# the same launcher works interactively.

function Invoke-PythonCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Prefix = @(),
        [string[]]$Arguments = @()
    )
    if ([string]::IsNullOrWhiteSpace($Executable)) {
        throw 'No Python executable was selected.'
    }
    $allArguments = @()
    if ($Prefix.Count -gt 0) { $allArguments += $Prefix }
    if ($Arguments.Count -gt 0) { $allArguments += $Arguments }

    # Native stderr is data here (pip warnings, probe tracebacks, bridge device
    # errors). Do not let a caller's Stop preference turn it into a script abort.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Executable @allArguments
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Format-PythonCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @()
    )
    $exeText = $Executable
    if ($Executable.IndexOf(' ') -ge 0 -or $Executable.IndexOf("`t") -ge 0) {
        $escaped = $Executable.Replace("'", "''")
        $exeText = "& '$escaped'"
    }
    $parts = @($exeText)
    if ($Arguments.Count -gt 0) { $parts += $Arguments }
    return ($parts -join ' ')
}

function Get-PythonCandidates {
    param(
        [string]$RepoRoot = '',
        [string]$PythonOverride = ''
    )
    $candidates = [System.Collections.Generic.List[object]]::new()
    if ($PythonOverride) {
        $candidates.Add([pscustomobject]@{ Exe = $PythonOverride; Args = @() })
    }
    if ($RepoRoot) {
        foreach ($venv in @('.venv', 'venv')) {
            $exe = Join-Path (Join-Path $RepoRoot $venv) 'Scripts\python.exe'
            if (Test-Path -LiteralPath $exe) {
                $candidates.Add([pscustomobject]@{ Exe = $exe; Args = @() })
            }
        }
    }
    if (Get-Command 'py' -ErrorAction SilentlyContinue) {
        $candidates.Add([pscustomobject]@{ Exe = 'py'; Args = @('-3') })
        $candidates.Add([pscustomobject]@{ Exe = 'py'; Args = @() })
    }
    foreach ($name in @('python', 'python3')) {
        if (Get-Command $name -ErrorAction SilentlyContinue) {
            $candidates.Add([pscustomobject]@{ Exe = $name; Args = @() })
        }
    }
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Programs\Python') }
    if ($env:ProgramFiles) { $roots += $env:ProgramFiles }
    foreach ($root in $roots) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        foreach ($pattern in @('Python3*\python.exe', 'Python3*\python3.exe')) {
            $path = Join-Path $root $pattern
            foreach ($exe in @(Get-ChildItem -Path $path -ErrorAction SilentlyContinue)) {
                if ($exe.PSIsContainer) { continue }
                $candidates.Add([pscustomobject]@{ Exe = $exe.FullName; Args = @() })
            }
        }
    }
    return $candidates.ToArray()
}

function Test-PythonInterpreter {
    param([Parameter(Mandatory = $true)]$Candidate)
    try {
        $output = Invoke-PythonCommand -Executable $Candidate.Exe -Prefix $Candidate.Args -Arguments @(
            '-c', 'import sys; print(sys.version_info[0], sys.version_info[1])'
        ) 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
    $line = [string]($output | Select-Object -Last 1)
    $parts = $line.Trim().Split(' ')
    if ($parts.Count -lt 2) { return $null }
    $major = 0
    $minor = 0
    if (-not [int]::TryParse($parts[0], [ref]$major)) { return $null }
    if (-not [int]::TryParse($parts[1], [ref]$minor)) { return $null }
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) { return $null }
    return "$major.$minor"
}

function Test-PhonebridgeImportable {
    param([Parameter(Mandatory = $true)]$Candidate)
    try {
        Invoke-PythonCommand -Executable $Candidate.Exe -Prefix $Candidate.Args -Arguments @(
            '-c', 'import phonebridge'
        ) 2>$null | Out-Null
    } catch {
        return $false
    }
    return ($LASTEXITCODE -eq 0)
}

function Resolve-Python {
    param(
        [string]$RepoRoot = '',
        [string]$PythonOverride = ''
    )
    $fallback = $null
    foreach ($candidate in @(Get-PythonCandidates -RepoRoot $RepoRoot -PythonOverride $PythonOverride)) {
        $version = Test-PythonInterpreter -Candidate $candidate
        if (-not $version) { continue }
        $hasPackage = Test-PhonebridgeImportable -Candidate $candidate
        $resolved = [pscustomobject]@{
            Exe        = $candidate.Exe
            Args       = @($candidate.Args)
            Version    = $version
            HasPackage = $hasPackage
        }
        if ($hasPackage) { return $resolved }
        if (-not $fallback) { $fallback = $resolved }
    }
    return $fallback
}
