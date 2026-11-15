param(
    [ValidateSet('helper', 'module', 'resolve')][string]$Mode = 'helper'
)

$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) { throw 'Run this helper from its file path.' }
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $repoRoot 'scripts\python-command.ps1')

# The regression is specifically the Windows py launcher form: executable and
# argument are separate values, never the single command name 'py -3'.
$pyExe = 'py'
$pyArgs = @('-3')

switch ($Mode) {
    'helper' {
        $code = 'import json,sys; print(sys.version_info[0], sys.version_info[1]); print(json.dumps(sys.argv[1:]))'
        Invoke-PythonCommand -Executable $pyExe -Prefix $pyArgs -Arguments @('-c', $code, 'marker with spaces', 'second marker')
        exit $LASTEXITCODE
    }
    'module' {
        Invoke-PythonCommand -Executable $pyExe -Prefix $pyArgs -Arguments @('-m', 'phonebridge', '--help')
        exit $LASTEXITCODE
    }
    'resolve' {
        $resolved = Resolve-Python -RepoRoot $repoRoot
        if (-not $resolved) { throw 'Resolve-Python found no usable Python on the Windows runner.' }
        $result = [ordered]@{
            exe        = $resolved.Exe
            args       = @($resolved.Args)
            version    = $resolved.Version
            hasPackage = $resolved.HasPackage
        }
        $result | ConvertTo-Json -Depth 4
    }
}
