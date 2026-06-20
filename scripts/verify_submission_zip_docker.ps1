param(
    [string]$Zip = ".codex_workspace/dist/ogc_solver_submission.zip",
    [string]$Image = "ubuntu:24.04"
)

$ErrorActionPreference = "Stop"

$Docker = Get-Command docker.exe -ErrorAction SilentlyContinue
if (-not $Docker) {
    throw "docker.exe not found. Install Docker Desktop and make sure Docker is on PATH."
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LinuxZip = $Zip -replace "\\", "/"
$VerifyCommand = @(
    "apt-get update >/dev/null",
    "apt-get install -y --no-install-recommends unzip file >/dev/null",
    "rm -rf /tmp/ogc_ziptest",
    "mkdir /tmp/ogc_ziptest",
    "unzip -q '$LinuxZip' -d /tmp/ogc_ziptest",
    "stat -c '%a %A %n' /tmp/ogc_ziptest/ogc_solver/cpp_accel/ogc_fast_solver",
    "file /tmp/ogc_ziptest/ogc_solver/cpp_accel/ogc_fast_solver"
) -join " && "

& docker run --rm `
    -v "${Root}:/work" `
    -w /work `
    $Image `
    bash -lc $VerifyCommand

if ($LASTEXITCODE -ne 0) {
    throw "Docker verification failed with exit code $LASTEXITCODE."
}
