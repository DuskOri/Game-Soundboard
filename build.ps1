$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$distDir = Join-Path $projectDir "dist-new"
$specDir = Join-Path $projectDir "build-spec"
$workDir = Join-Path $projectDir "build-new"
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
New-Item -ItemType Directory -Path $specDir -Force | Out-Null

Push-Location $projectDir
try {
    python -m PyInstaller --noconfirm --clean --windowed --uac-admin --onedir `
        --name "GameSoundboard" `
        --specpath "$specDir" `
        --distpath "$distDir" `
        --workpath "$workDir" `
        --add-binary "$ffmpeg;." `
        --collect-all sounddevice `
        --collect-all tkinterdnd2 `
        app.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host "Build complete: $distDir\GameSoundboard\GameSoundboard.exe"
