# Sets up EG Player for running from source:
#   - creates .venv (Python 3.10+) and installs PySide6-Essentials, python-mpv, PyInstaller
#   - downloads libmpv (shinchiro/mpv-winbuild-cmake) and FFmpeg (BtbN/FFmpeg-Builds, GPL shared) into .\bin
#   - with -InstallerTools: installs Inno Setup 7, GitHub CLI and Git for the current user (used by release.ps1)
# Usage:  powershell -ExecutionPolicy Bypass -File setup.ps1 [-InstallerTools]
param([switch]$InstallerTools)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = $PSScriptRoot
$bin = Join-Path $root "bin"
New-Item -ItemType Directory -Force $bin | Out-Null

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    $py = (Get-Command py -ErrorAction SilentlyContinue)
    if ($py) { py -3 -m venv "$root\.venv" } else { python -m venv "$root\.venv" }
}
& "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$root\.venv\Scripts\python.exe" -m pip install -r "$root\requirements.txt"

$tmp = Join-Path $env:TEMP ("egplayer_setup_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $tmp | Out-Null
$hdr = @{ "User-Agent" = "egplayer-setup" }
try {
    if (-not (Test-Path "$bin\libmpv-2.dll")) {
        Write-Host "Downloading libmpv..."
        $rel = Invoke-RestMethod "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest" -Headers $hdr
        $asset = $rel.assets | Where-Object { $_.name -match '^mpv-dev-x86_64-\d{8}-git-.*\.7z$' } | Select-Object -First 1
        Invoke-WebRequest $asset.browser_download_url -OutFile "$tmp\mpv-dev.7z"
        New-Item -ItemType Directory -Force "$tmp\mpv" | Out-Null
        tar -xf "$tmp\mpv-dev.7z" -C "$tmp\mpv"
        Copy-Item "$tmp\mpv\libmpv-2.dll" $bin
    }
    if (-not (Test-Path "$bin\ffmpeg.exe")) {
        Write-Host "Downloading FFmpeg..."
        $rel = Invoke-RestMethod "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest" -Headers $hdr
        $asset = $rel.assets | Where-Object { $_.name -match '^ffmpeg-n[\d.]+-latest-win64-gpl-shared-[\d.]+\.zip$' } |
            Sort-Object name -Descending | Select-Object -First 1
        Invoke-WebRequest $asset.browser_download_url -OutFile "$tmp\ffmpeg.zip"
        New-Item -ItemType Directory -Force "$tmp\ff" | Out-Null
        tar -xf "$tmp\ffmpeg.zip" -C "$tmp\ff"
        $ffbin = Get-ChildItem -Recurse "$tmp\ff" -Directory | Where-Object Name -eq "bin" | Select-Object -First 1
        Get-ChildItem $ffbin.FullName | Where-Object { $_.Name -ne "ffplay.exe" } | Copy-Item -Destination $bin
    }
    $haveIscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
                  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ }
    if ($InstallerTools -and -not $haveIscc) {
        Write-Host "Downloading Inno Setup (official jrsoftware release)..."
        $rel = Invoke-RestMethod "https://api.github.com/repos/jrsoftware/issrc/releases/latest" -Headers $hdr
        $asset = $rel.assets | Where-Object { $_.name -match '^innosetup-[\d.]+-x64\.exe$' } | Select-Object -First 1
        $exe = "$tmp\$($asset.name)"
        Invoke-WebRequest $asset.browser_download_url -OutFile $exe
        if ((Get-AuthenticodeSignature $exe).Status -ne "Valid") { throw "Inno Setup download has no valid signature" }
        Start-Process $exe -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-","/CURRENTUSER" -Wait
    }
    # GitHub CLI + MinGit (portable, per-user) so release.ps1 can publish to GitHub Releases
    $tools = @(
        @{ Name = "GitHub CLI"; Repo = "cli/cli"; Pattern = '^gh_[\d.]+_windows_amd64\.zip$'
           Dir = "$env:LOCALAPPDATA\Programs\GitHub CLI"; Bin = "bin"; Exe = "gh" },
        @{ Name = "Git (MinGit)"; Repo = "git-for-windows/git"; Pattern = '^MinGit-[\d.]+-64-bit\.zip$'
           Dir = "$env:LOCALAPPDATA\Programs\MinGit"; Bin = "cmd"; Exe = "git" })
    foreach ($t in $tools) {
        if (-not $InstallerTools -or (Get-Command $t.Exe -ErrorAction SilentlyContinue) -or
            (Test-Path (Join-Path $t.Dir "$($t.Bin)\$($t.Exe).exe"))) { continue }
        Write-Host "Downloading $($t.Name) (official $($t.Repo) release)..."
        $rel = Invoke-RestMethod "https://api.github.com/repos/$($t.Repo)/releases/latest" -Headers $hdr
        $asset = $rel.assets | Where-Object { $_.name -match $t.Pattern } | Select-Object -First 1
        $zip = "$tmp\$($asset.name)"
        Invoke-WebRequest $asset.browser_download_url -OutFile $zip
        $want = ("" + $asset.digest) -replace '^sha256:', ''
        if (-not $want -or (Get-FileHash $zip -Algorithm SHA256).Hash -ne $want.ToUpper()) {
            throw "$($t.Name) download failed its SHA-256 check"
        }
        Expand-Archive $zip -DestinationPath $t.Dir -Force
        $userPath = "" + [Environment]::GetEnvironmentVariable("Path", "User")
        $add = Join-Path $t.Dir $t.Bin
        if (($userPath -split ';') -notcontains $add) {
            [Environment]::SetEnvironmentVariable("Path", (($userPath.TrimEnd(';'), $add) -join ';'), "User")
        }
    }
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
Write-Host "Done. Start with:  .venv\Scripts\pythonw.exe run.pyw   (or run build.ps1 for EGPlayer.exe)"
