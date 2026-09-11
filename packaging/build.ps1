$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = "C:\Python313\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$TesseractCommand = Get-Command tesseract -ErrorAction SilentlyContinue
$TesseractExe = if ($TesseractCommand) {
    $TesseractCommand.Source
} else {
    "C:\Program Files\Tesseract-OCR\tesseract.exe"
}
if (-not (Test-Path -LiteralPath $TesseractExe)) {
    throw "Tesseract is required to build portable scanned-resume OCR support."
}
$TesseractRoot = Split-Path -Parent $TesseractExe
$TesseractEnglish = Join-Path $TesseractRoot "tessdata\eng.traineddata"
if (-not (Test-Path -LiteralPath $TesseractEnglish)) {
    throw "Tesseract English language data was not found."
}

$OutputRoot = Join-Path $ProjectRoot "packaging\out"
$BackendDist = Join-Path $OutputRoot "backend-dist"
$FrontendDist = Join-Path $OutputRoot "frontend-dist"
$ReleaseDir = Join-Path $ProjectRoot "release"
New-Item -ItemType Directory -Force -Path $OutputRoot, $ReleaseDir | Out-Null
$ResolvedOutputRoot = [IO.Path]::GetFullPath($OutputRoot + [IO.Path]::DirectorySeparatorChar)
# Interrupted older builds can leave a generated lookup-config.env behind.
# Remove only our GUID-named private directories inside packaging\out before
# creating the next one; the authoritative .env is never touched here.
Get-ChildItem -LiteralPath $OutputRoot -Directory -Filter ".MedhuntPrivateBuild-*" | ForEach-Object {
    $StalePrivateDir = [IO.Path]::GetFullPath($_.FullName)
    if ($StalePrivateDir.StartsWith($ResolvedOutputRoot, [StringComparison]::OrdinalIgnoreCase) -and
        $_.Name.StartsWith(".MedhuntPrivateBuild-")) {
        Remove-Item -LiteralPath $StalePrivateDir -Recurse -Force
    }
}
$PrivateBuildDir = Join-Path $OutputRoot (".MedhuntPrivateBuild-" + [Guid]::NewGuid().ToString("N"))
$BundledLookupConfig = Join-Path $PrivateBuildDir "lookup-config.env"
New-Item -ItemType Directory -Path $PrivateBuildDir | Out-Null

Push-Location $ProjectRoot
try {
    & $Python (Join-Path $ProjectRoot "packaging\provision_local_config.py") `
        --source (Join-Path $ProjectRoot ".env") `
        --override (Join-Path $ProjectRoot ".env.local") `
        --destination $BundledLookupConfig `
        --require-primary `
        --require-secondary `
        --require-quick-sourcer `
        --require-nexus `
        --require-cloud
    if ($LASTEXITCODE -ne 0) { throw "Trusted lookup configuration could not be prepared." }

    & $Python (Join-Path $ProjectRoot "packaging\build_frontend.py") `
        --source (Join-Path $ProjectRoot "src_pkg\frontend") `
        --output $FrontendDist
    if ($LASTEXITCODE -ne 0) { throw "Frontend production build failed with exit code $LASTEXITCODE" }

    & $Python -m PyInstaller --noconfirm --clean --windowed `
        --name RadixsolBackend `
        --distpath $BackendDist `
        --workpath (Join-Path $OutputRoot "backend-build") `
        --specpath $OutputRoot `
        --paths (Join-Path $ProjectRoot "src_pkg") `
        --add-data "$FrontendDist;frontend" `
        --collect-all psycopg `
        --collect-all psycopg_binary `
        --collect-all pydantic_core `
        --collect-all boto3 `
        --collect-all botocore `
        --collect-all pypdf `
        --collect-all reportlab `
        --hidden-import pypdfium2 `
        --collect-all pypdfium2_raw `
        --add-binary "$TesseractExe;tesseract" `
        --add-binary "$(Join-Path $TesseractRoot '*.dll');tesseract" `
        --add-data "$TesseractEnglish;tesseract/tessdata" `
        --additional-hooks-dir (Join-Path $ProjectRoot "packaging\hooks") `
        --exclude-module google.genai.tests `
        --exclude-module pytest `
        --hidden-import uvicorn.logging `
        --hidden-import uvicorn.loops.auto `
        --hidden-import uvicorn.protocols.http.auto `
        --hidden-import uvicorn.protocols.websockets.auto `
        --hidden-import uvicorn.lifespan.on `
        --version-file (Join-Path $ProjectRoot "packaging\version_backend.txt") `
        (Join-Path $ProjectRoot "src_pkg\backend_launcher.py")
    if ($LASTEXITCODE -ne 0) { throw "Backend packaging failed with exit code $LASTEXITCODE" }

    $BackendPayload = Join-Path $BackendDist "RadixsolBackend"
    if (-not (Test-Path -LiteralPath (Join-Path $BackendPayload "RadixsolBackend.exe"))) {
        throw "Packaged backend executable was not produced."
    }
    $BundledTessdata = Join-Path $BackendPayload "_internal\tesseract\tessdata\eng.traineddata"
    if (-not (Test-Path -LiteralPath $BundledTessdata)) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $BundledTessdata) | Out-Null
        Copy-Item -LiteralPath $TesseractEnglish -Destination $BundledTessdata -Force
    }
    $BundledPydanticCore = Get-ChildItem `
        -LiteralPath (Join-Path $BackendPayload "_internal\pydantic_core") `
        -Filter "_pydantic_core*.pyd" -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $BundledPydanticCore) {
        throw "Packaged backend is missing the compiled pydantic_core module."
    }

    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name Medhunt-Setup `
        --distpath $ReleaseDir `
        --workpath (Join-Path $OutputRoot "setup-build") `
        --specpath $OutputRoot `
        --add-data "$BackendPayload;payload/backend" `
        --add-data "$FrontendDist;payload/extension" `
        --add-data "$(Join-Path $ProjectRoot 'packaging\Install Extension.html');payload" `
        --add-data "$(Join-Path $ProjectRoot 'packaging\TEAM_CONFIGURATION.txt');payload" `
        --add-data "$BundledLookupConfig;payload" `
        --version-file (Join-Path $ProjectRoot "packaging\version_setup.txt") `
        (Join-Path $ProjectRoot "packaging\setup_installer.py")
    if ($LASTEXITCODE -ne 0) { throw "Setup packaging failed with exit code $LASTEXITCODE" }

    $Setup = Join-Path $ReleaseDir "Medhunt-Setup.exe"
    if (-not (Test-Path -LiteralPath $Setup)) { throw "Medhunt-Setup.exe was not produced." }
    $LegacySetup = Join-Path $ReleaseDir "Radixsol-Setup.exe"
    if (Test-Path -LiteralPath $LegacySetup) {
        Remove-Item -LiteralPath $LegacySetup -Force
    }
    $Hash = Get-FileHash -Algorithm SHA256 -LiteralPath $Setup
    $Info = Get-Item -LiteralPath $Setup
    [PSCustomObject]@{
        File = $Info.FullName
        Bytes = $Info.Length
        SHA256 = $Hash.Hash
    } | Format-List
}
finally {
    Pop-Location
    $ResolvedPrivateBuildDir = [IO.Path]::GetFullPath($PrivateBuildDir)
    $ResolvedOutputRoot = [IO.Path]::GetFullPath($OutputRoot + [IO.Path]::DirectorySeparatorChar)
    if ($ResolvedPrivateBuildDir.StartsWith($ResolvedOutputRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $ResolvedPrivateBuildDir).StartsWith(".MedhuntPrivateBuild-")) {
        Remove-Item -LiteralPath $ResolvedPrivateBuildDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
