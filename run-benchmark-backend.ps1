$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceRoot = Join-Path $projectRoot "src_pkg"
Set-Location -LiteralPath $sourceRoot
python -m uvicorn api:app --host 127.0.0.1 --port 8091
