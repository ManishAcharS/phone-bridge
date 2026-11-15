param(
    [string]$Python = '',
    [switch]$UsePyLauncher,
    [string]$LoopbackName = 'CABLE'
)

$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) { throw 'Run this helper from its file path.' }
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $repoRoot 'scripts\python-command.ps1')
. (Join-Path $repoRoot 'scripts\python-probes.ps1')
if ($UsePyLauncher) {
    $script:PythonExe = 'py'
    $script:PythonArgs = @('-3')
} elseif ($Python) {
    $script:PythonExe = $Python
    $script:PythonArgs = @()
} else {
    $script:PythonExe = 'python'
    $script:PythonArgs = @()
}

$result = [ordered]@{
    packages = Test-PythonPackages
    cable = Test-CableInstalled
    loopback = Test-LoopbackChoice -Name $LoopbackName
}
$result | ConvertTo-Json -Depth 4
