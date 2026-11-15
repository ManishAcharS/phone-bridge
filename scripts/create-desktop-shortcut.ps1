<#
.SYNOPSIS
    Create a desktop shortcut for the PhoneBridge daily launcher.

.DESCRIPTION
    Writes one .lnk to the current user's Desktop. The shortcut runs
    scripts\start-live.bat through cmd.exe /k so the window stays open after an
    error and the exact fix remains readable. No administrator rights, no PATH
    change, no execution-policy change, and no other folder is touched.

.PARAMETER Name
    Shortcut file name without .lnk. Default: PhoneBridge.

.PARAMETER Arguments
    Arguments passed to start-live.bat, e.g. '-Transport wifi -HostName 192.168.1.20'.

.PARAMETER Force
    Overwrite an existing shortcut with the same name. Without this, the script
    refuses rather than silently replacing a user's shortcut.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\create-desktop-shortcut.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\create-desktop-shortcut.ps1 -Name "PhoneBridge Wi-Fi" -Arguments "-Transport wifi"
#>
[CmdletBinding()]
param(
    [string]$Name = 'PhoneBridge',
    [string]$Arguments = '',
    [string]$IconLocation = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) { throw 'Run this script from its file path, not from a pasted block.' }
if ([string]::IsNullOrWhiteSpace($Name)) { throw 'Name cannot be empty.' }
$invalid = [System.IO.Path]::GetInvalidFileNameChars()
foreach ($char in $invalid) {
    if ($Name.IndexOf($char) -ge 0) { throw "Name contains an invalid file-name character: $char" }
}
if ($Arguments.Contains("`r") -or $Arguments.Contains("`n")) {
    throw 'Arguments must be one line.'
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $PSScriptRoot 'start-live.bat'
if (-not (Test-Path -LiteralPath $bat)) { throw "Launcher not found: $bat" }
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { throw 'Windows did not report a Desktop folder for this user.' }
$linkPath = Join-Path $desktop ($Name + '.lnk')
if ((Test-Path -LiteralPath $linkPath) -and -not $Force) {
    throw "Shortcut already exists: $linkPath. Remove it first or rerun with -Force."
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$cmd = $env:ComSpec
if (-not $cmd) { $cmd = 'cmd.exe' }
$shortcut.TargetPath = $cmd
# /k keeps the console open after the launcher exits, so a missing OBS/VB-CABLE
# or phone error does not vanish before it can be read.
$shortcut.Arguments = '/k ""' + $bat + '" ' + $Arguments + '"'
$shortcut.WorkingDirectory = $repoRoot
$shortcut.Description = 'PhoneBridge daily camera + microphone + speaker launcher'
if ($IconLocation) { $shortcut.IconLocation = $IconLocation }
$shortcut.Save()

Write-Host "Created: $linkPath"
Write-Host "Target:  $($shortcut.TargetPath) $($shortcut.Arguments)"
Write-Host 'The window stays open after an error; read the fix, then close it or type exit.'
