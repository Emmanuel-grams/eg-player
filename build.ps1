# Builds a standalone dist\EGPlayer\EGPlayer.exe (no Python needed on the target PC).
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
# To make an installer + update manifest as well, use release.ps1 instead.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Run setup.ps1 first." }
if (-not (Test-Path "$root\bin\libmpv-2.dll")) { throw "bin\libmpv-2.dll missing - run setup.ps1 first." }

& $py "$root\tools\make_icon.py"
$version = & $py "$root\tools\make_version_file.py"
Push-Location $root
try {
    & $py -m PyInstaller --noconfirm --clean --windowed --name EGPlayer `
        --icon "$root\assets\egplayer.ico" `
        --version-file "$root\build\version_info.txt" `
        --distpath "$root\dist" --workpath "$root\build" --specpath "$root\build" `
        --exclude-module tkinter --exclude-module unittest --exclude-module pydoc `
        "$root\run.pyw"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    $out = "$root\dist\EGPlayer"
    New-Item -ItemType Directory -Force "$out\bin" | Out-Null
    Copy-Item "$root\bin\*" "$out\bin" -Force
    Copy-Item "$root\assets" "$out" -Recurse -Force
    Copy-Item "$root\README.md", "$root\LICENSE" "$out" -Force
    $size = (Get-ChildItem -Recurse $out | Measure-Object Length -Sum).Sum / 1MB
    Write-Host ("Built EG Player {0}: {1}\EGPlayer.exe  ({2:N0} MB folder)" -f $version, $out, $size)
} finally {
    Pop-Location
    Remove-Item -Recurse -Force "$root\build" -ErrorAction SilentlyContinue
}
