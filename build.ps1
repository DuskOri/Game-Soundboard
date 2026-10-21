$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildDir = Join-Path $projectDir "build"
$distDir = Join-Path $projectDir "dist\current"
$specDir = Join-Path $buildDir "spec"
$workDir = Join-Path $buildDir "work"
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
$catalog = Join-Path $projectDir "soundpacks\catalog.json"
New-Item -ItemType Directory -Path $specDir -Force | Out-Null

Push-Location $projectDir
try {
    python -m PyInstaller --noconfirm --clean --windowed --uac-admin --onedir `
        --name "GameSoundboard" `
        --specpath "$specDir" `
        --distpath "$distDir" `
        --workpath "$workDir" `
        --add-binary "$ffmpeg;." `
        --add-data "$catalog;soundpacks" `
        --collect-all sounddevice `
        --collect-all tkinterdnd2 `
        app.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$tkdndDir = Join-Path $distDir "GameSoundboard\_internal\tkinterdnd2\tkdnd"
if (Test-Path $tkdndDir) {
    Get-ChildItem $tkdndDir -Directory |
        Where-Object Name -ne "win-x64" |
        Remove-Item -Recurse -Force
}

Write-Host "Build complete: $distDir\GameSoundboard\GameSoundboard.exe"
