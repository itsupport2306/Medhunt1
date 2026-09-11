$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = "C:\Python313\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$OutputRoot = Join-Path $ProjectRoot "packaging\out"
$WatcherDist = Join-Path $OutputRoot "watcher-dist"
$ReleaseDir = Join-Path $ProjectRoot "release"
$SetupWork = Join-Path $OutputRoot "watcher-setup-build"
$SetupSpec = Join-Path $OutputRoot "watcher-setup-spec"

New-Item -ItemType Directory -Force -Path $OutputRoot, $ReleaseDir | Out-Null
& $Python (Join-Path $ProjectRoot "packaging\build_watcher_extension.py") `
    --project $ProjectRoot --output $WatcherDist
if ($LASTEXITCODE -ne 0) { throw "Watcher extension build failed." }

& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name Medhunt-Watcher-Setup `
    --distpath $ReleaseDir `
    --workpath $SetupWork `
    --specpath $SetupSpec `
    --collect-all dotenv `
    --add-data "$WatcherDist;payload/watcher" `
    --add-data "$(Join-Path $ProjectRoot 'packaging\Install Watcher.html');payload" `
    --version-file (Join-Path $ProjectRoot "packaging\version_watcher_setup.txt") `
    (Join-Path $ProjectRoot "packaging\watcher_setup.py")
if ($LASTEXITCODE -ne 0) { throw "Watcher setup build failed." }

$Setup = Get-Item (Join-Path $ReleaseDir "Medhunt-Watcher-Setup.exe")
[PSCustomObject]@{
    File = $Setup.FullName
    Version = $Setup.VersionInfo.FileVersion
    Bytes = $Setup.Length
    SHA256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Setup.FullName).Hash
} | Format-List
