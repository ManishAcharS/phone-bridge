param(
    [string]$Python = 'python',
    [string]$LoopbackName = 'CABLE'
)

$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) { throw 'Run this helper from its file path.' }
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$probePath = Join-Path $PSScriptRoot '..\..\scripts\python-probes.ps1'
. $probePath
$script:PythonCommand = @($Python)

$result = [ordered]@{
    packages = Test-PythonPackages
    cable = Test-CableInstalled
    loopback = Test-LoopbackChoice -Name $LoopbackName
}
$result | ConvertTo-Json -Depth 4
