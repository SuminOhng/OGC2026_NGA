param(
    [string]$Image = "ubuntu:24.04",
    [string]$Output = ".codex_workspace/dist/ogc_solver_submission.zip"
)

$ErrorActionPreference = "Stop"

$Docker = Get-Command docker.exe -ErrorAction SilentlyContinue
if (-not $Docker) {
    throw "docker.exe not found. Install Docker Desktop and make sure Docker is on PATH."
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LinuxOutput = $Output -replace "\\", "/"
$BuildCommand = @(
    "apt-get update",
    "apt-get install -y --no-install-recommends g++ python3 ca-certificates",
    "python3 scripts/build_cpp_accelerator.py --submission",
    "chmod 755 ogc_solver/ogc_solver/cpp_accel/ogc_fast_solver",
    "python3 scripts/make_submission.py --require-native --minimal-cpp --output '$LinuxOutput'"
) -join " && "

& docker run --rm `
    -v "${Root}:/work" `
    -w /work `
    $Image `
    bash -lc $BuildCommand

if ($LASTEXITCODE -ne 0) {
    throw "Docker build failed with exit code $LASTEXITCODE."
}

$OutputPath = Resolve-Path (Join-Path $Root $Output)
Write-Host "Official C++ submission zip created: $OutputPath"
