<#
.SYNOPSIS
    Build the shared Lambda Layer for BTG ConnectAI (Python 3.13) on Windows.

.DESCRIPTION
    Produces layer/python/ with the src/shared/ package plus the runtime
    dependencies from src/requirements.txt, then zips it to
    layer/shared-layer.zip.

    Dependencies are cross-compiled for the Lambda runtime via
    `pip --platform manylinux2014_x86_64`. For fully reproducible artifacts
    prefer build.sh on Linux/CI.
#>
$ErrorActionPreference = "Stop"

$PythonVersion = "3.13"
$LayerDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $LayerDir
$PythonTarget = Join-Path $LayerDir "python"
$Artifact = Join-Path $LayerDir "shared-layer.zip"

Write-Host "==> Cleaning previous build"
if (Test-Path $PythonTarget) { Remove-Item -Recurse -Force $PythonTarget }
if (Test-Path $Artifact) { Remove-Item -Force $Artifact }
New-Item -ItemType Directory -Path $PythonTarget | Out-Null

Write-Host "==> Installing runtime dependencies (python $PythonVersion)"
pip install `
    --requirement (Join-Path $RepoRoot "src/requirements.txt") `
    --target $PythonTarget `
    --python-version $PythonVersion `
    --only-binary=:all: `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --upgrade

Write-Host "==> Copying shared package"
Copy-Item -Recurse -Force (Join-Path $RepoRoot "src/shared") (Join-Path $PythonTarget "shared")

Write-Host "==> Pruning bytecode / caches"
Get-ChildItem -Path $PythonTarget -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force

Write-Host "==> Zipping layer -> $Artifact"
# Archive the `python` directory itself so it sits at the zip root, as required
# by the Lambda Python layer convention.
Compress-Archive -Path $PythonTarget -DestinationPath $Artifact -Force

Write-Host "==> Done: $Artifact"
