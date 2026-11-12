# PhoneBridge virtual camera - read-only prerequisite check
#
# Answers four questions and changes nothing:
#   1. which Windows build this is, and whether the Media Foundation virtual
#      camera API exists on it,
#   2. whether the MSVC x64 C++ tools are installed (and where),
#   3. which Windows SDKs are installed, and whether one of them has
#      mfvirtualcamera.h,
#   4. whether CMake is available.
#
# It does NOT: register the camera, write a registry key, install anything,
# change an execution policy (the .bat passes -ExecutionPolicy Bypass for this
# one process only), or need administrator rights.

$ErrorActionPreference = 'Continue'
$missing = New-Object System.Collections.Generic.List[string]

function Write-Section([string]$title) {
    Write-Host ''
    Write-Host ('== ' + $title + ' ' + ('=' * [Math]::Max(2, 64 - $title.Length)))
}

function Write-Ok([string]$text)   { Write-Host ('  [ ok ] ' + $text) }
function Write-Bad([string]$text)  { Write-Host ('  [missing] ' + $text) }
function Write-Info([string]$text) { Write-Host ('  ' + $text) }

$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
if (-not $repoRoot -or -not (Test-Path (Join-Path $repoRoot 'windows-native\vcam-poc\CMakeLists.txt'))) {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}

Write-Host 'PhoneBridge virtual camera - prerequisite check (read only)'
Write-Host ('  script: ' + $PSCommandPath)

# ---------------------------------------------------------------------------
Write-Section 'Windows'
$build = 0
$versionLabel = 'unknown'
try {
    $key = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop
    $build = [int]$key.CurrentBuildNumber
    $versionLabel = '' + $key.ProductName + ' ' + $key.DisplayVersion
    if ($key.PSObject.Properties.Name -contains 'UBR') { $versionLabel += '.' + $key.UBR }
} catch {
    Write-Bad 'could not read the build number from the registry'
}
if ($build -gt 0) {
    Write-Info ('version:  ' + $versionLabel)
    Write-Info ('build:    ' + $build)
    if ($build -ge 22000) {
        Write-Ok 'build 22000 or later: MFCreateVirtualCamera is documented as available'
    } else {
        Write-Bad ('build ' + $build + ' is below 22000, the documented floor for MFCreateVirtualCamera')
        $missing.Add('Windows 11 build 22000 or later (this machine reports ' + $build + ')')
    }
}
if ([Environment]::Is64BitOperatingSystem) {
    Write-Ok ('64-bit OS (' + $env:PROCESSOR_ARCHITECTURE + ')')
} else {
    Write-Bad 'the virtual camera API is x64/arm64 only; this OS is 32-bit'
    $missing.Add('64-bit Windows')
}

# ---------------------------------------------------------------------------
Write-Section 'Media Foundation camera API (mfsensorgroup.dll)'
$sensorGroup = Join-Path $env:WINDIR 'System32\mfsensorgroup.dll'
if (Test-Path $sensorGroup) {
    $fileVersion = (Get-Item $sensorGroup).VersionInfo.FileVersion
    Write-Ok ('present: ' + $sensorGroup + ' (file version ' + $fileVersion + ')')
    # The export name is ASCII inside the PE export table, so a byte search is a
    # reliable existence test and needs no compiler.
    $found = & findstr /M /C:"MFCreateVirtualCamera" "$sensorGroup" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'the DLL exports MFCreateVirtualCamera'
    } else {
        Write-Bad 'MFCreateVirtualCamera is not exported by this DLL'
        $missing.Add('mfsensorgroup.dll exporting MFCreateVirtualCamera')
    }
} else {
    Write-Bad ('missing: ' + $sensorGroup)
    $missing.Add('mfsensorgroup.dll (comes with Windows 11 22000+)')
}

# ---------------------------------------------------------------------------
Write-Section 'Visual Studio 2022 C++ tools (MSVC x64)'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsPaths = @()
if (Test-Path $vswhere) {
    $vsPaths = @(& $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null)
    $vsPaths = $vsPaths | Where-Object { $_ -and $_.Trim() -ne '' }
}
if ($vsPaths.Count -eq 0) {
    Write-Bad 'no Visual Studio installation with "Desktop development with C++" was found'
    $missing.Add('Visual Studio 2022 with the "Desktop development with C++" workload')
} else {
    foreach ($path in $vsPaths) {
        Write-Ok ('Visual Studio at ' + $path)
        $msvcRoot = Join-Path $path 'VC\Tools\MSVC'
        if (Test-Path $msvcRoot) {
            $toolsets = Get-ChildItem $msvcRoot -Directory | Sort-Object Name -Descending
            if ($toolsets.Count -gt 0) {
                Write-Info ('  MSVC toolset: ' + $toolsets[0].Name)
                if ($toolsets.Count -gt 1) { Write-Info ('  also: ' + (($toolsets[1..($toolsets.Count-1)] | ForEach-Object { $_.Name }) -join ', ')) }
            }
        } else {
            Write-Bad '  no VC\Tools\MSVC directory: the C++ workload is not installed'
            $missing.Add('MSVC C++ tools under ' + $path)
        }
    }
}

# ---------------------------------------------------------------------------
Write-Section 'Windows SDK (needs mfvirtualcamera.h)'
$includeRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Include'
$usableSdk = $null
$sdkWithHeader = @()
if (Test-Path $includeRoot) {
    $sdks = Get-ChildItem $includeRoot -Directory | Sort-Object Name -Descending
    foreach ($sdk in $sdks) {
        $header = Join-Path $sdk.FullName 'um\mfvirtualcamera.h'
        if (Test-Path $header) {
            Write-Ok ('SDK ' + $sdk.Name + ' has um\mfvirtualcamera.h')
            $sdkWithHeader += $sdk.Name
            if (-not $usableSdk) { $usableSdk = $sdk.Name }
        } else {
            Write-Info ('SDK ' + $sdk.Name + ' (no mfvirtualcamera.h)')
        }
    }
    if ($sdkWithHeader.Count -eq 0) {
        Write-Bad 'no installed SDK contains mfvirtualcamera.h'
        $missing.Add('Windows 11 SDK 10.0.22000 or later, with mfvirtualcamera.h')
    }
} else {
    Write-Bad ('no Windows SDK include directory at ' + $includeRoot)
    $missing.Add('Windows 11 SDK 10.0.22000 or later')
}

# ---------------------------------------------------------------------------
Write-Section 'CMake'
$cmakeCommand = Get-Command cmake -ErrorAction SilentlyContinue
$cmakePath = $null
if ($cmakeCommand) { $cmakePath = $cmakeCommand.Source }
if (-not $cmakePath) {
    foreach ($path in $vsPaths) {
        $candidate = Join-Path $path 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
        if (Test-Path $candidate) { $cmakePath = $candidate; break }
    }
}
if ($cmakePath) {
    $cmakeVersionLine = (& $cmakePath --version | Select-Object -First 1)
    Write-Ok ($cmakeVersionLine + '  (' + $cmakePath + ')')
    if ($cmakeVersionLine -match '(\d+)\.(\d+)\.(\d+)') {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 20)) {
            Write-Bad 'CMake 3.20 or later is required'
            $missing.Add('CMake 3.20 or later (found ' + $cmakeVersionLine + ')')
        }
    }
} else {
    Write-Bad 'cmake was not found on PATH or in the Visual Studio installation'
    $missing.Add('CMake 3.20 or later')
}

# ---------------------------------------------------------------------------
Write-Section 'Sources on disk'
$pocRoot = Join-Path $repoRoot 'windows-native\vcam-poc'
$expected = @(
    'CMakeLists.txt',
    'src\media_source.h',
    'src\media_source.cpp',
    'src\media_source_activate.h',
    'src\media_source_activate.cpp',
    'src\vcam.def',
    'src\vcam_host.cpp'
)
foreach ($relative in $expected) {
    $full = Join-Path $pocRoot $relative
    if (Test-Path $full) { Write-Ok $relative } else { Write-Bad $relative }
}
$buildDir = Join-Path $repoRoot 'build\vcam'
if (Test-Path $buildDir) {
    Write-Info ('an existing build directory is at ' + $buildDir + ' (delete it to rebuild from scratch)')
} else {
    Write-Info 'no build directory yet: nothing has been built here'
}

# ---------------------------------------------------------------------------
Write-Section 'Verdict'
if ($missing.Count -eq 0) {
    Write-Ok 'every prerequisite is present - the POC can be built'
    Write-Host ''
    Write-Host '  Next (from the repository root, in an x64 Native Tools prompt):'
    Write-Host ('    cmake -S windows-native\vcam-poc -B build\vcam -G "Visual Studio 17 2022" -A x64' + $(if ($usableSdk) { ' -DCMAKE_SYSTEM_VERSION=' + $usableSdk } else { '' }))
    Write-Host '    cmake --build build\vcam --config Release'
    Write-Host '    build\vcam\Release\vcam_host.exe --check'
    Write-Host ''
    Write-Host '  --check only reports whether the API is reachable; it registers nothing.'
} else {
    Write-Bad 'missing prerequisites:'
    foreach ($item in $missing) { Write-Host ('    - ' + $item) }
    Write-Host ''
    Write-Host '  Nothing was changed by this script. Install the items above, then run it again.'
}
Write-Host ''
