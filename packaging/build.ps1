# Stage a one-folder CreatorAssistant release, and optionally zip it.
# Does not read or copy the developer config.yaml (secrets stay out of the zip).
param(
    [switch]$StageOnly,
    [switch]$ZipOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if ($StageOnly -and $ZipOnly) {
    throw "Use only one of -StageOnly or -ZipOnly."
}

function Get-AppVersion {
    $text = Get-Content -Raw -Path (Join-Path $Root "version.py")
    if ($text -notmatch '__version__\s*=\s*"([^"]+)"') {
        throw "version.py is missing __version__"
    }
    return $Matches[1]
}

function Find-VcVars64 {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) {
        throw "vswhere.exe not found. Install Visual Studio with the Desktop development with C++ workload."
    }
    $install = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $install) {
        throw "Visual Studio C++ tools not found."
    }
    $vcvars = Join-Path $install "VC\Auxiliary\Build\vcvars64.bat"
    if (-not (Test-Path $vcvars)) {
        throw "vcvars64.bat not found at $vcvars"
    }
    return $vcvars
}

$Version = Get-AppVersion
$Tag = "v$Version"
$StageParent = Join-Path $Root "dist\CreatorAssistant-$Tag-win64"
$Stage = Join-Path $StageParent "CreatorAssistant"
$Zip = Join-Path $Root "dist\CreatorAssistant-$Tag-win64.zip"
$Sum = "$Zip.sha256"

function New-ReleaseZip {
    if (-not (Test-Path (Join-Path $Stage "CreatorAssistant.exe"))) {
        throw "Stage folder is missing. Run packaging/build.ps1 -StageOnly first."
    }
    $log = Join-Path $Stage "creatorassistant.log"
    if (Test-Path $log) {
        Remove-Item $log -Force
    }
    if (Test-Path $Zip) { Remove-Item $Zip -Force }
    if (Test-Path $Sum) { Remove-Item $Sum -Force }
    & tar.exe -a -c -f $Zip -C $StageParent "CreatorAssistant"
    if ($LASTEXITCODE -ne 0) {
        throw "tar failed with exit $LASTEXITCODE"
    }
    $hash = (Get-FileHash -Algorithm SHA256 -Path $Zip).Hash
    $name = Split-Path $Zip -Leaf
    Set-Content -Path $Sum -Value "$hash  $name" -Encoding ascii
    Write-Host "Wrote $Zip"
    Write-Host "Wrote $Sum"
}

if ($ZipOnly) {
    New-ReleaseZip
    exit 0
}

function Find-Python {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch "WindowsApps") {
        return $cmd.Source
    }
    foreach ($candidate in @(
        (Join-Path $env:LocalAppData "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe")
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw "Python 3.13 was not found. Install it, or open a shell where 'python' is the real interpreter."
}

$VenvPython = Join-Path $Root "packaging\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $BasePython = Find-Python
    & $BasePython -m venv (Join-Path $Root "packaging\.venv")
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed ($LASTEXITCODE)" }
}
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed ($LASTEXITCODE)" }
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt") "pyinstaller>=6.0" "obsws-python"
if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }

& $VenvPython -m PyInstaller --noconfirm (Join-Path $Root "CreatorAssistant.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller CreatorAssistant.spec failed ($LASTEXITCODE)" }
& $VenvPython -m PyInstaller --noconfirm (Join-Path $Root "CreatorAssistantGameLogger.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller CreatorAssistantGameLogger.spec failed ($LASTEXITCODE)" }

$coreDir = Join-Path $Root "dist\CreatorAssistant-core"
$coreExe = Join-Path $coreDir "CreatorAssistant-core.exe"
$loggerExe = Join-Path $Root "dist\CreatorAssistantGameLogger.exe"
if (-not (Test-Path $coreExe)) { throw "Missing $coreExe" }
if (-not (Test-Path (Join-Path $coreDir "_internal\python313.dll"))) {
    throw "Missing _internal\python313.dll. The core build is not a one-folder PyInstaller 6 layout."
}
if (-not (Test-Path $loggerExe)) { throw "Missing $loggerExe" }

if (Test-Path $StageParent) {
    Remove-Item $StageParent -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Copy-Item $coreExe $Stage
Copy-Item (Join-Path $coreDir "_internal") (Join-Path $Stage "_internal") -Recurse
Copy-Item $loggerExe $Stage
Copy-Item (Join-Path $Root "config.example.yaml") (Join-Path $Stage "config.yaml")
Copy-Item (Join-Path $Root "config.example.yaml") (Join-Path $Stage "config.example.yaml")
Copy-Item (Join-Path $Root "README.md") (Join-Path $Stage "README.md")

$vcvars = Find-VcVars64
$objDir = Join-Path $Root "build\launcher"
New-Item -ItemType Directory -Force -Path $objDir | Out-Null
$launcherOut = Join-Path $Stage "CreatorAssistant.exe"
$src = Join-Path $PSScriptRoot "launcher.c"
$obj = Join-Path $objDir "launcher.obj"
$compile = "call `"$vcvars`" && cl /nologo /O1 /W3 /Fo:`"$obj`" /Fe:`"$launcherOut`" `"$src`" user32.lib /link /SUBSYSTEM:WINDOWS"
cmd.exe /c $compile
if ($LASTEXITCODE -ne 0) { throw "launcher compile failed ($LASTEXITCODE)" }
if (-not (Test-Path $launcherOut)) { throw "launcher exe was not created" }

Write-Host "Staged $Stage"
if (-not $StageOnly) {
    New-ReleaseZip
}
