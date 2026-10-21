# Builds the firmware locally with Docker, same targets as GitHub Actions (build.yaml).
# Usage:  .\build-local.ps1          build everything
#         .\build-local.ps1 left     only targets whose name contains "left"
# Output: ..\firmware\*.uf2  (outside the repo, so it never gets committed)
param([string]$Only = '')

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$out = Join-Path (Split-Path $repo -Parent) 'firmware'
$image = 'zmkfirmware/zmk-build-arm:3.5'   # ZMK v0.3 = Zephyr 3.5

docker info *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop and try again.' }

New-Item -ItemType Directory -Force $out | Out-Null

docker run --rm `
    -v zmk-workspace:/workspace `
    -v "${repo}:/src:ro" `
    -v "${out}:/out" `
    $image bash /src/scripts/local_build.sh $Only

if ($LASTEXITCODE -ne 0) { throw "Build failed (exit code $LASTEXITCODE)." }
Get-ChildItem $out -Filter *.uf2 | Select-Object Name, LastWriteTime, Length
