# Makes a new EG Player release:
#   1. stamps the version into egplayer\version.py
#   2. builds dist\EGPlayer\EGPlayer.exe (build.ps1)
#   3. compiles releases\EGPlayer-Setup-<version>.exe with Inno Setup
#   4. writes releases\latest.json (version, installer, SHA-256, notes) for the in-app updater
#   5. when the update source is a GitHub repository: commits and pushes the code, tags it
#      v<version> and publishes a GitHub Release with the Setup exe and latest.json attached
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File release.ps1 -Version 1.2.0 -Notes "What changed"
#   ... -LocalOnly                          # build into releases\ only, publish nothing
#   ... -UpdateSource "yourname/eg-player"  # bake a different default update source into the app
#
# Every installed copy checks the update source when it starts and offers the new version.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = "",
    [string]$UpdateSource = "",
    [switch]$LocalOnly,
    [int]$Keep = 2          # how many installers to keep in releases\ (older ones are deleted)
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must look like 1.2.0" }
$tag = "v$Version"

function Find-Tool($name, $candidates) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
# Windows PowerShell turns a native tool's redirected stderr into errors; run quiet checks with that off.
function Test-Quiet($exe, [string[]]$arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $exe @arguments *> $null } finally { $ErrorActionPreference = $old }
    return ($LASTEXITCODE -eq 0)
}
function Invoke-Tool($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw ("failed: {0} {1}" -f (Split-Path $exe -Leaf), ($arguments -join " ")) }
}

# ---- 0. where this release goes
$vfile = "$root\egplayer\version.py"
$cur = Get-Content $vfile -Raw
if (-not $UpdateSource) {
    $m = [regex]::Match($cur, 'DEFAULT_UPDATE_SOURCE\s*=\s*r?"([^"]*)"')
    $UpdateSource = if ($m.Success) { $m.Groups[1].Value } else { "$root\releases" }
}
$repo = $null
$gm = [regex]::Match($UpdateSource, '^(?:github:|https?://(?:www\.)?github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$')
if (-not $LocalOnly -and $gm.Success -and -not (Test-Path $UpdateSource)) {
    $repo = "{0}/{1}" -f $gm.Groups[1].Value, $gm.Groups[2].Value
    $gh = Find-Tool gh @("$env:LOCALAPPDATA\Programs\GitHub CLI\bin\gh.exe", "$env:ProgramFiles\GitHub CLI\gh.exe")
    if (-not $gh) { throw "GitHub CLI (gh) not found - run setup.ps1 -InstallerTools, or add -LocalOnly" }
    if (-not (Test-Quiet $gh @("auth", "status", "--hostname", "github.com"))) {
        throw "GitHub CLI is not signed in - run:  gh auth login --web   (or add -LocalOnly)"
    }
    if (Test-Quiet $gh @("release", "view", $tag, "--repo", $repo)) { throw "$repo already has a release $tag" }
    $git = Find-Tool git @("$env:LOCALAPPDATA\Programs\MinGit\cmd\git.exe", "$env:ProgramFiles\Git\cmd\git.exe")
    $useGit = $git -and (Test-Path "$root\.git")
    if ($useGit -and (Test-Quiet $git @("-C", $root, "rev-parse", "-q", "--verify", "refs/tags/$tag"))) {
        throw "git tag $tag already exists"
    }
    Write-Host "This release will be published to GitHub: https://github.com/$repo/releases/tag/$tag"
}

# ---- 1. version stamp
$vtext = @"
# Written by release.ps1 - the single source of the app version and the default update source.
__version__ = "$Version"

# Where "Check for updates" looks by default. Can be changed in the app (Help > Check for updates).
# Accepts a GitHub repository ("owner/repo"), a URL to latest.json, or a folder with the installers.
DEFAULT_UPDATE_SOURCE = r"$UpdateSource"
"@
[IO.File]::WriteAllText($vfile, $vtext + "`n", (New-Object Text.UTF8Encoding($false)))

# ---- 2. app build
& powershell -ExecutionPolicy Bypass -File "$root\build.ps1"
if ($LASTEXITCODE -ne 0) { throw "build.ps1 failed" }

# ---- 3. installer
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 7\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup (ISCC.exe) not found - run setup.ps1 -InstallerTools" }
& "$root\.venv\Scripts\python.exe" "$root\tools\gen_installer_assoc.py"
New-Item -ItemType Directory -Force "$root\releases" | Out-Null
& $iscc /Qp "/DAppVersion=$Version" "$root\installer\EGPlayer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed" }
$setup = "$root\releases\EGPlayer-Setup-$Version.exe"

# ---- 4. update manifest
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $setup).Length
$manifest = [ordered]@{
    version   = $Version
    installer = "EGPlayer-Setup-$Version.exe"
    sha256    = $hash
    size      = $size
    date      = (Get-Date -Format "yyyy-MM-dd")
    notes     = $Notes
}
$json = $manifest | ConvertTo-Json
[IO.File]::WriteAllText("$root\releases\latest.json", $json, (New-Object Text.UTF8Encoding($false)))

$old = Get-ChildItem "$root\releases" -Filter "EGPlayer-Setup-*.exe" |
    Where-Object { $_.Name -match '^EGPlayer-Setup-(\d+\.\d+\.\d+)\.exe$' } |
    Sort-Object { [version]($_.Name -replace '^EGPlayer-Setup-|\.exe$', '') } -Descending |
    Select-Object -Skip ([Math]::Max(1, $Keep))
$old | ForEach-Object { Remove-Item $_.FullName; Write-Host "  removed old installer $($_.Name)" }

# ---- 5. GitHub
if ($repo) {
    if ($useGit) {
        Write-Host "Pushing the code to GitHub..."
        Invoke-Tool $git @("-C", $root, "add", "-A")
        if (-not (Test-Quiet $git @("-C", $root, "diff", "--cached", "--quiet"))) {
            Invoke-Tool $git @("-C", $root, "commit", "-q", "-m", "EG Player $Version")
        }
        Invoke-Tool $git @("-C", $root, "tag", "-a", $tag, "-m", "EG Player $Version")
        Invoke-Tool $git @("-C", $root, "push", "-q", "origin", "HEAD")
        Invoke-Tool $git @("-C", $root, "push", "-q", "origin", $tag)
    }
    Write-Host ("Uploading the installer to GitHub ({0:N0} MB)..." -f ($size / 1MB))
    $nf = Join-Path $env:TEMP "egplayer-release-notes.md"
    $body = if ($Notes.Trim()) { $Notes } else { "EG Player $Version" }
    [IO.File]::WriteAllText($nf, $body, (New-Object Text.UTF8Encoding($false)))
    try {
        $relArgs = @("release", "create", $tag, $setup, "$root\releases\latest.json", "--repo", $repo,
                     "--title", "EG Player $Version", "--notes-file", $nf, "--latest")
        if ($useGit) { $relArgs += "--verify-tag" }
        Invoke-Tool $gh $relArgs
    } finally {
        Remove-Item $nf -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host ("Release {0} ready:" -f $Version)
Write-Host ("  {0}  ({1:N1} MB)" -f $setup, ($size / 1MB))
Write-Host ("  {0}\releases\latest.json  (sha256 {1})" -f $root, $hash)
if ($repo) {
    Write-Host "  https://github.com/$repo/releases/tag/$tag"
    Write-Host "Every installed copy offers the update the next time it starts."
} else {
    Write-Host "Installed copies whose update source is this releases folder will offer the update now."
}
